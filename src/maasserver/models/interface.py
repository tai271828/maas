# Copyright 2015-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Model for interfaces."""

__all__ = [
    'BondInterface',
    'build_vlan_interface_name',
    'PhysicalInterface',
    'Interface',
    'VLANInterface',
    'UnknownInterface',
    ]

from django.contrib.postgres.fields import ArrayField
from django.core.exceptions import (
    PermissionDenied,
    ValidationError,
)
from django.db.models import (
    BooleanField,
    CharField,
    ForeignKey,
    Manager,
    ManyToManyField,
    PROTECT,
    Q,
    TextField,
)
from django.db.models.query import QuerySet
from maasserver import DefaultMeta
from maasserver.enum import (
    INTERFACE_LINK_TYPE,
    INTERFACE_TYPE,
    INTERFACE_TYPE_CHOICES,
    IPADDRESS_TYPE,
)
from maasserver.exceptions import (
    StaticIPAddressOutOfRange,
    StaticIPAddressUnavailable,
)
from maasserver.fields import (
    JSONObjectField,
    MACAddressField,
    VerboseRegexValidator,
)
from maasserver.models.cleansave import CleanSave
from maasserver.models.staticipaddress import StaticIPAddress
from maasserver.models.timestampedmodel import TimestampedModel
from maasserver.utils.orm import (
    get_one,
    MAASQueriesMixin,
)
from netaddr import (
    AddrFormatError,
    IPAddress,
    IPNetwork,
)
from provisioningserver.logger import get_maas_logger
from provisioningserver.utils.network import parse_integer


maaslog = get_maas_logger("interface")

# This is only last-resort validation, more specialized validation
# will happen at the form level based on the interface type.
INTERFACE_NAME_REGEXP = '^[\w\-_.:]+$'

# Default value for bridge_fd.
DEFAULT_BRIDGE_FD = 15


def get_subnet_family(subnet):
    """Return the IPADDRESS_FAMILY for the `subnet`."""
    if subnet is not None:
        return subnet.get_ipnetwork().version
    else:
        return None


class InterfaceQueriesMixin(MAASQueriesMixin):

    def get_specifiers_q(self, specifiers, separator=':', **kwargs):
        """Returns a Q object for objects matching the given specifiers.

        :return:django.db.models.Q
        """
        # Circular imports.
        from maasserver.models import (
            Fabric,
            Subnet,
            VLAN,
        )

        # This dict is used by the constraints code to identify objects
        # with particular properties. Please note that changing the keys here
        # can impact backward compatibility, so use caution.
        specifier_types = {
            None: self._add_default_query,
            'id': self._add_interface_id_query,
            'fabric': (Fabric.objects, 'vlan__interface'),
            'fabric_class': 'vlan__fabric__class_type',
            'ip': 'ip_addresses__ip',
            'mode': self._add_mode_query,
            'name': '__name',
            'hostname': 'node__hostname',
            'subnet': (Subnet.objects, 'staticipaddress__interface'),
            'space': 'subnet{s}space'.format(s=separator),
            'subnet_cidr': 'subnet{s}cidr'.format(s=separator),
            'type': '__type',
            'vlan': (VLAN.objects, 'interface'),
            'vid': self._add_vlan_vid_query,
            'tag': self._add_tag_query,
        }
        return super(InterfaceQueriesMixin, self).get_specifiers_q(
            specifiers, specifier_types=specifier_types, separator=separator,
            **kwargs)

    def _add_interface_id_query(self, current_q, op, item):
        try:
            item = parse_integer(item)
        except ValueError:
            raise ValidationError("Interface ID must be numeric.")
        else:
            return op(current_q, Q(id=item))

    def _add_default_query(self, current_q, op, item):
        # First, just try treating this as an interface ID.
        try:
            object_id = parse_integer(item)
        except ValueError:
            pass
        else:
            return op(current_q, Q(id=object_id))

        if '/' in item:
            # The user may have tried to pass in a CIDR.
            # That means we need to check both the IP address and the subnet's
            # CIDR.
            try:
                ip_cidr = IPNetwork(item)
            except (AddrFormatError, ValueError):
                pass
            else:
                cidr = str(ip_cidr.cidr)
                ip = str(ip_cidr.ip)
                return op(current_q, Q(
                    ip_addresses__ip=ip, ip_addresses__subnet__cidr=cidr))
        else:
            # Check if the user passed in an IP address.
            try:
                ip = IPAddress(item)
            except (AddrFormatError, ValueError):
                pass
            else:
                return op(current_q, Q(ip_addresses__ip=str(ip)))

        # If all else fails, try the interface name.
        return op(current_q, Q(name=item))

    def _add_mode_query(self, current_q, op, item):
        if item.strip().lower() != 'unconfigured':
            raise ValidationError(
                "The only valid value for 'mode' is 'unconfigured'.")
        return op(
            current_q,
            Q(ip_addresses__ip__isnull=True) | Q(ip_addresses__ip=''))

    def _add_tag_query(self, current_q, op, item):
        # Allow item to instruct AND in the filter. eg: tag:e1000&&sriov.
        items = item.split("&&")
        return op(current_q, Q(tags__contains=items))

    def get_matching_node_map(self, specifiers):
        """Returns a tuple where the first element is a set of matching node
        IDs, and the second element is a dictionary mapping a node ID to a list
        of matching interfaces, such as:

        {
            <node1>: [<interface1>, <interface2>, ...]
            <node2>: [<interface3>, ...]
            ...
        }

        :returns: tuple (set, dict)
        """
        return super(InterfaceQueriesMixin, self).get_matching_object_map(
            specifiers, 'node__id')


class InterfaceQuerySet(InterfaceQueriesMixin, QuerySet):
    """Custom QuerySet which mixes in some additional queries specific to
    subnets. This needs to be a mixin because an identical method is needed on
    both the Manager and all QuerySets which result from calling the manager.
    """


class InterfaceManager(Manager, InterfaceQueriesMixin):
    """A Django manager managing one type of interface."""

    def get_queryset(self):
        """Return the `QuerySet` for the `Interface`s.

        If this manager is on `PhysicalInterface`, `BondInterface`,
        or `VLANInterface` it will filter only allow returning those
        interfaces.
        """
        qs = InterfaceQuerySet(self.model, using=self._db)
        interface_type = self.model.get_type()
        if interface_type is None:
            # Not a specific interface type so we don't filter by the type.
            return qs
        else:
            return qs.filter(type=interface_type)

    def get_interfaces_on_node_by_name(self, node, interface_names):
        """Returns a list of Inteface objects on the specified node whose
        names match the specified list of interface names.
        """
        return list(self.filter(node=node, name__in=interface_names))

    def filter_by_ip(self, static_ip_address):
        """Given the specified StaticIPAddress, (or string containing an IP
        address) return the Interface it is on.
        """
        if isinstance(static_ip_address, str):
            static_ip_address = StaticIPAddress.objects.get(
                ip=static_ip_address)
        return self.filter(ip_addresses=static_ip_address)

    def get_interface_or_404(self, system_id, specifiers, user, perm):
        """Fetch a `Interface` by its `Node`'s system_id and its id.  Raise
        exceptions if no `Interface` with this id exist, if the `Node` with
        system_id doesn't exist, if the `Interface` doesn't exist on the
        `Node`, or if the provided user has not the required permission on
        this `Node` and `Interface`.

        :param system_id: The system_id.
        :type system_id: string
        :param specifiers: The interface specifier.
        :type specifiers: str
        :param user: The user that should be used in the permission check.
        :type user: django.contrib.auth.models.User
        :param perm: The permission to assert that the user has on the node.
        :type perm: str
        :raises: django.http.Http404_,
            :class:`maasserver.exceptions.PermissionDenied`.

        .. _django.http.Http404: https://
           docs.djangoproject.com/en/dev/topics/http/views/
           #the-http404-exception
        """
        interface = self.get_object_by_specifiers_or_raise(
            specifiers, node__system_id=system_id)
        node = interface.get_node()
        if user.has_perm(perm, interface) and user.has_perm(perm, node):
            return interface
        else:
            raise PermissionDenied()

    def get_or_create(self, *args, **kwargs):
        """Get or create `Interface`.

        Provides logic to allow `get_or_create` to be called with a `parents`
        in kwargs. `parents` is used to find an already existing interface that
        is a child of first parent. If a child of the first parent exists that
        matches the other parameters then that interface is returned and not
        created. If no match can be found then the interface is created and
        assigned to a child or all of the `parents`.
        """
        parents = kwargs.pop("parents", [])
        matching_kwargs = dict(kwargs)
        matching_kwargs.pop("defaults", None)

        def matches(interface):
            for key, value in matching_kwargs.items():
                if getattr(interface, key) != value:
                    return False
            return True

        if len(parents) > 0:
            parent = parents[0]
            for rel in parent.children_relationships.all():
                if matches(rel.child):
                    return rel.child, False

        interface, created = super(InterfaceManager, self).get_or_create(
            *args, **kwargs)
        if created:
            for parent in parents:
                InterfaceRelationship(child=interface, parent=parent).save()
            # Need to call save again to update the fields on the interface.
            interface.save()
        return interface, created

    def get_or_create_on_node(self, node, name, mac_address, parent_nics):
        """Create an interface on the specified node, with the specified MAC
        address and parent NICs.

        This method is necessary because get_or_create() often offers too
        simplistic an approach to interface matching. For example, if an
        interface has been moved, its MAC has changed, or its dependencies
        on other interfaces have changed.

        This method attempts to update an existing interface, if a
        match can be found. Otherwise, a new interface will be created.

        If the interface being created is a replacement for an interface that
        already exists, the caller is responsible for deleting it.
        """
        if self.model.get_type() == INTERFACE_TYPE.PHYSICAL:
            # Physical interfaces have a MAC uniqueness restriction.
            interfaces = self.get_queryset().filter(
                Q(mac_address=mac_address) | Q(name=name) & Q(node=node))
            interface = interfaces.first()
        else:
            interfaces = self.get_queryset().filter(
                Q(name=name) & Q(node=node) & Q(type=self.model.get_type()))
            interface = interfaces.first()
        if interface is not None:
            if (interface.type != self.model.get_type() and
                    interface.node.id == node.id):
                # This means we found the interface on this node, but the type
                # didn't match what we expected. This should not happen unless
                # we changed the modeling of this interface type, or the admin
                # intentionally changed the interface type.
                interface.delete()
                return self.get_or_create_on_node(
                    node, name, mac_address, parent_nics)
            interface.mac_address = mac_address
            interface.name = name
            interface.parents.clear()
            for parent_nic in parent_nics:
                InterfaceRelationship(
                    child=interface, parent=parent_nic).save()
            if interface.node.id != node.id:
                # Bond with MAC address was on a different node. We need to
                # move it to its new owner. In the process we delete all of its
                # current links because they are completely wrong.
                interface.ip_addresses.all().delete()
                interface.node = node
        else:
            interface = self.create(
                name=name, mac_address=mac_address, node=node)
            for parent_nic in parent_nics:
                InterfaceRelationship(
                    child=interface, parent=parent_nic).save()
        return interface


class Interface(CleanSave, TimestampedModel):

    class Meta(DefaultMeta):
        verbose_name = "Interface"
        verbose_name_plural = "Interfaces"
        ordering = ('created', )

    objects = InterfaceManager()

    node = ForeignKey('Node', editable=False, null=True, blank=True)

    name = CharField(
        blank=False, editable=True, max_length=255,
        validators=[VerboseRegexValidator(INTERFACE_NAME_REGEXP)],
        help_text="Interface name.")

    type = CharField(
        max_length=20, editable=False, choices=INTERFACE_TYPE_CHOICES,
        blank=False)

    parents = ManyToManyField(
        'self', blank=True, editable=True,
        through='InterfaceRelationship', symmetrical=False)

    vlan = ForeignKey(
        'VLAN', editable=True, blank=True, null=True, on_delete=PROTECT)

    ip_addresses = ManyToManyField(
        'StaticIPAddress', editable=True, blank=True)

    mac_address = MACAddressField(unique=False, null=True, blank=True)

    ipv4_params = JSONObjectField(blank=True, default="")

    ipv6_params = JSONObjectField(blank=True, default="")

    params = JSONObjectField(blank=True, default="")

    tags = ArrayField(
        TextField(), blank=True, null=True, default=list)

    enabled = BooleanField(default=True)

    # Set only on a BridgeInterface when it is created by a standard user
    # when a machine is acquired. Once the machine is released the bridge
    # interface is removed.
    acquired = BooleanField(default=False, editable=False)

    def __init__(self, *args, **kwargs):
        type = kwargs.get('type', self.get_type())
        kwargs['type'] = type
        # Derive the concrete class from the interface's type.
        super(Interface, self).__init__(*args, **kwargs)
        klass = INTERFACE_TYPE_MAPPING.get(self.type)
        if klass:
            self.__class__ = klass
        else:
            raise ValueError("Unknown interface type: %s" % type)

    @classmethod
    def get_type(cls):
        """Get the type of interface for this class.

        Return `None` on `Interface`.
        """
        return None

    def __str__(self):
        return "name=%s, type=%s, mac=%s" % (
            self.name, self.type, self.mac_address)

    def get_node(self):
        return self.node

    def get_log_string(self):
        hostname = "<unknown-node>"
        node = self.get_node()
        if node is not None:
            hostname = node.hostname
        return "%s (%s) on %s" % (self.get_name(), self.type, hostname)

    def get_name(self):
        return self.name

    def is_enabled(self):
        return self.enabled

    def get_effective_mtu(self):
        """Return the effective MTU value for this interface."""
        mtu = None
        if self.params:
            mtu = self.params.get('mtu', None)
        if mtu is None and self.vlan is not None:
            mtu = self.vlan.mtu
        if mtu is None:
            # Use default MTU for the interface when the interface has no
            # MTU set and it is disconnected.
            from maasserver.models.vlan import DEFAULT_MTU
            mtu = DEFAULT_MTU
        return mtu

    def get_links(self):
        """Return the definition of links connected to this interface.

        Example definition::

          {
              "id": 1,
              "mode": "dhcp",
              "ip_address": "192.168.1.2",
              "subnet": <Subnet object>
          }

        ``ip`` and ``subnet`` are optional and are only present if the
        `StaticIPAddress` has an IP address and/or subnet.
        """
        links = []
        for ip_address in self.ip_addresses.exclude(
                alloc_type=IPADDRESS_TYPE.DISCOVERED):
            link_type = ip_address.get_interface_link_type()
            link = {
                "id": ip_address.id,
                "mode": link_type,
            }
            ip, subnet = ip_address.get_ip_and_subnet()
            if ip:
                link["ip_address"] = "%s" % ip
            if subnet:
                link["subnet"] = subnet
            links.append(link)
        return links

    def get_discovered(self):
        """Return the definition of discovered IPs belonging to this interface.

        Example definition::

          {
              "ip_address": "192.168.1.2",
              "subnet": <Subnet object>
          }

        """
        discovered_ips = self.ip_addresses.filter(
            alloc_type=IPADDRESS_TYPE.DISCOVERED)
        if len(discovered_ips) > 0:
            discovered = []
            for discovered_ip in discovered_ips:
                if discovered_ip.ip is not None and discovered_ip.ip != "":
                    discovered.append({
                        "subnet": discovered_ip.subnet,
                        "ip_address": "%s" % discovered_ip.ip,
                        })
            return discovered
        else:
            return None

    def only_has_link_up(self):
        """Return True if this interface is only set to LINK_UP."""
        ip_addresses = self.ip_addresses.exclude(
            alloc_type=IPADDRESS_TYPE.DISCOVERED)
        link_modes = set(
            ip.get_interface_link_type()
            for ip in ip_addresses
        )
        return link_modes == set([INTERFACE_LINK_TYPE.LINK_UP])

    def is_configured(self):
        """Return True if the interface is enabled and has at least one link
        that is not a LINK_UP."""
        if not self.is_enabled():
            return False
        # We do the removal of DISCOVERED here instead of in a query so that
        # prefetch_related can be used on the interface query.
        link_modes = set(
            ip.get_interface_link_type()
            for ip in self.ip_addresses.all()
            if ip.alloc_type != IPADDRESS_TYPE.DISCOVERED
        )
        return (
            len(link_modes) != 0 and
            link_modes != set([INTERFACE_LINK_TYPE.LINK_UP]))

    def update_ip_addresses(self, cidr_list):
        """Update the IP addresses linked to this interface.

        This only updates the DISCOVERED IP addresses connected to this
        interface. All other IPADDRESS_TYPE's are left alone.

        :param cidr_list: A list of IP/network addresses using the CIDR format
            e.g. ``['192.168.12.1/24', 'fe80::9e4e:36ff:fe3b:1c94/64']`` to
            which the interface is connected.
        """
        # Circular imports.
        from maasserver.models import StaticIPAddress, Subnet

        # Delete all current DISCOVERED IP address on this interface. As new
        # ones are about to be added.
        StaticIPAddress.objects.filter(
            interface=self, alloc_type=IPADDRESS_TYPE.DISCOVERED).delete()

        for ip in cidr_list:
            network = IPNetwork(ip)
            cidr = str(network.cidr)
            address = str(network.ip)

            # Find the Subnet for each IP address seen (will be used later
            # to create or update the Subnet)
            try:
                subnet = Subnet.objects.get(cidr=cidr)
                # Update VLAN based on the VLAN this Subnet belongs to.
                if subnet.vlan != self.vlan:
                    vlan = subnet.vlan
                    maaslog.info("%s: Observed connected to %s via %s." % (
                        self.get_log_string(), vlan.fabric.get_name(), cidr))
                    self.vlan = vlan
                    self.save()
            except Subnet.DoesNotExist:
                # XXX mpontillo 2015-11-01 Configuration != state. That is,
                # this means we have just a subnet on an unknown Fabric/VLAN,
                # and this fact should be recorded somewhere, so that the user
                # gets a chance to configure it. Note, however, that if this
                # is already a managed cluster interface, a Fabric/VLAN will
                # already have been created.
                subnet = Subnet.objects.create_from_cidr(cidr)
                maaslog.info(
                    "Creating subnet %s connected to interface %s "
                    "of node %s.", cidr, self, self.get_node())

            # First check if this IP address exists in the database (at all).
            prev_address = get_one(
                StaticIPAddress.objects.filter(ip=address))
            if prev_address is not None:
                if prev_address.alloc_type == IPADDRESS_TYPE.DISCOVERED:
                    # Previous address was a discovered address so we can
                    # delete it without and messages.
                    if prev_address.is_linked_to_one_unknown_interface():
                        prev_address.interface_set.all().delete()
                    prev_address.delete()
                elif prev_address.interface_set.count() == 0:
                    # Previous address is just hanging around, this should not
                    # happen. But just in-case we delete the IP address.
                    prev_address.delete()
                else:
                    if subnet.vlan.dhcp_on:
                        # Subnet is managed by MAAS and the IP address is
                        # not DISCOVERED then we have a big problem as MAAS
                        # should not allow IP address to be allocated in a
                        # managed dynamic range.
                        alloc_name = prev_address.get_log_name_for_alloc_type()
                        node = prev_address.get_node()
                        maaslog.warning(
                            "%s IP address (%s)%s was deleted because "
                            "it was handed out by the MAAS DHCP server "
                            "from the dynamic range.",
                            alloc_name, prev_address.ip,
                            " on " + node.fqdn if node is not None else '')
                        prev_address.delete()
                    else:
                        # This is an external DHCP server where the subnet is
                        # not managed by MAAS. It is possible that the user
                        # did something wrong and set a static IP address on
                        # another node to an IP address inside the same range
                        # that the DHCP server provides.
                        alloc_name = prev_address.get_log_name_for_alloc_type()
                        node = prev_address.get_node()
                        maaslog.warning(
                            "%s IP address (%s)%s was deleted because "
                            "it was handed out by an external DHCP "
                            "server.",
                            alloc_name, prev_address.ip,
                            " on " + node.fqdn if node is not None else '')
                        prev_address.delete()

            # Create the newly discovered IP address.
            new_address = StaticIPAddress(
                alloc_type=IPADDRESS_TYPE.DISCOVERED, ip=address,
                subnet=subnet)
            new_address.save()
            self.ip_addresses.add(new_address)

    def _remove_link_dhcp(self, subnet_family=None):
        """Removes the DHCP links if they have no subnet or if the linked
        subnet is in the same `subnet_family`."""
        for ip in self.ip_addresses.all().select_related('subnet'):
            if (ip.alloc_type != IPADDRESS_TYPE.DISCOVERED and
                    ip.get_interface_link_type() == INTERFACE_LINK_TYPE.DHCP):
                if (subnet_family is None or ip.subnet is None or
                        ip.subnet.get_ipnetwork().version == subnet_family):
                    ip.delete()

    def _remove_link_up(self):
        """Removes the LINK_UP link if it exists."""
        for ip in self.ip_addresses.all():
            if (ip.alloc_type != IPADDRESS_TYPE.DISCOVERED and
                    ip.get_interface_link_type() == (
                        INTERFACE_LINK_TYPE.LINK_UP)):
                ip.delete()

    def _link_subnet_auto(self, subnet):
        """Link interface to subnet using AUTO."""
        ip_address = StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO, ip=None, subnet=subnet)
        ip_address.save()
        self.ip_addresses.add(ip_address)
        self._remove_link_dhcp(get_subnet_family(subnet))
        self._remove_link_up()
        return ip_address

    def _link_subnet_dhcp(self, subnet):
        """Link interface to subnet using DHCP."""
        self._remove_link_dhcp(get_subnet_family(subnet))
        ip_address = StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.DHCP, ip=None, subnet=subnet)
        ip_address.save()
        self.ip_addresses.add(ip_address)
        self._remove_link_up()
        return ip_address

    def _link_subnet_static(
            self, subnet, ip_address=None, alloc_type=None, user=None,
            swap_static_ip=None):
        """Link interface to subnet using STATIC."""
        valid_alloc_types = [
            IPADDRESS_TYPE.STICKY,
            IPADDRESS_TYPE.USER_RESERVED,
        ]
        if alloc_type is not None and alloc_type not in valid_alloc_types:
            raise ValueError(
                "Invalid alloc_type for STATIC mode: %s" % alloc_type)
        elif alloc_type is None:
            alloc_type = IPADDRESS_TYPE.STICKY
        if alloc_type == IPADDRESS_TYPE.STICKY:
            # Just to be sure STICKY always has a NULL user.
            user = None

        if ip_address:
            ip_address = IPAddress(ip_address)
            if subnet is not None:
                if ip_address not in subnet.get_ipnetwork():
                    raise StaticIPAddressOutOfRange(
                        "IP address is not in the given subnet '%s'." % subnet)
                ip_range = subnet.get_dynamic_range_for_ip(ip_address)
                if ip_range is not None:
                    raise StaticIPAddressOutOfRange(
                        "IP address is inside a dynamic range %s-%s." % (
                            ip_range.start_ip, ip_range.end_ip))

            # Try to get the requested IP address.
            static_ip, created = StaticIPAddress.objects.get_or_create(
                ip="%s" % ip_address,
                defaults={
                    'alloc_type': alloc_type,
                    'user': user,
                    'subnet': subnet,
                })
            if not created:
                raise StaticIPAddressUnavailable(
                    "IP address is already in use.")
            else:
                self.ip_addresses.add(static_ip)
                self.save()
        else:
            static_ip = StaticIPAddress.objects.allocate_new(
                subnet, alloc_type=alloc_type, user=user)
            self.ip_addresses.add(static_ip)

        # Swap the ID's that way it keeps the same ID as the swap object.
        if swap_static_ip is not None:
            static_ip.id, swap_static_ip.id = swap_static_ip.id, static_ip.id
            swap_static_ip.delete()
            static_ip.save()

        # Was successful at creating the STATIC link. Remove the DHCP and
        # LINK_UP link if it exists.
        self._remove_link_dhcp(get_subnet_family(subnet))
        self._remove_link_up()
        return static_ip

    def _link_subnet_link_up(self, subnet):
        """Link interface to subnet using LINK_UP."""
        self._remove_link_up()
        ip_address = StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY, ip=None, subnet=subnet)
        ip_address.save()
        self.ip_addresses.add(ip_address)
        self.save()
        self._remove_link_dhcp(get_subnet_family(subnet))
        return ip_address

    def link_subnet(
            self, mode, subnet, ip_address=None, alloc_type=None, user=None):
        """Link interface to subnet using the provided mode.

        :param mode: Mode of the link. See `INTERFACE_LINK_TYPE`
            for vocabulary.
        :param subnet: Subnet to link to. This can be None for `DHCP`
            and `LINK_UP`.
        :param ip_address: IP address in `subnet` to give to the interface.
            Only used for `STATIC` mode and if not given one will be selected.
        :param alloc_type: Allocation type for the link. This is only used for
            link mode STATIC.
        :param user: When alloc_type is set to USER_RESERVED, this user will
            be set on the link.
        """
        if mode == INTERFACE_LINK_TYPE.AUTO:
            return self._link_subnet_auto(subnet)
        elif mode == INTERFACE_LINK_TYPE.DHCP:
            return self._link_subnet_dhcp(subnet)
        elif mode == INTERFACE_LINK_TYPE.STATIC:
            return self._link_subnet_static(
                subnet, ip_address=ip_address,
                alloc_type=alloc_type, user=user)
        elif mode == INTERFACE_LINK_TYPE.LINK_UP:
            return self._link_subnet_link_up(subnet)
        else:
            raise ValueError("Unknown mode: %s" % mode)

    def force_auto_or_dhcp_link(self):
        """Force the interface to come up with an AUTO linked to a subnet on
        the same VLAN as the interface. If no subnet could be identified then
        its just set to DHCP.
        """
        # XXX mpontillo 2015-11-29: since we tend to dump a large number of
        # subnets into the default VLAN, this assumption might be incorrect in
        # many cases, leading to interfaces being configured as AUTO when
        # they should be configured as DHCP.
        if self.vlan is not None:
            found_subnet = self.vlan.subnet_set.first()
            if found_subnet is not None:
                return self.link_subnet(INTERFACE_LINK_TYPE.AUTO, found_subnet)
            else:
                return self.link_subnet(INTERFACE_LINK_TYPE.DHCP, None)

    def ensure_link_up(self):
        """Ensure that if no subnet links exists that at least a LINK_UP
        exists."""
        links = list(
            self.ip_addresses.exclude(alloc_type=IPADDRESS_TYPE.DISCOVERED))
        if len(links) > 0:
            # Ensure that LINK_UP only exists if no other links already exists.
            link_ups = []
            others = []
            for link in links:
                if link.alloc_type == IPADDRESS_TYPE.STICKY and (
                        link.ip is None or link.ip == ""):
                    link_ups.append(link)
                else:
                    others.append(link)
            if len(link_ups) > 0 and len(others) > 0:
                for link in link_ups:
                    link.delete()
        elif self.vlan is not None:
            # Use an associated subnet if it exists and its on the same VLAN
            # the interface is currently connected, else it will just be a
            # LINK_UP without a subnet.
            discovered_address = self.ip_addresses.filter(
                alloc_type=IPADDRESS_TYPE.DISCOVERED,
                subnet__vlan=self.vlan).first()
            if discovered_address is not None:
                subnet = discovered_address.subnet
            else:
                subnet = None
            self.link_subnet(INTERFACE_LINK_TYPE.LINK_UP, subnet)

    def _unlink_static_ip(self, static_ip, swap_alloc_type=None):
        """Unlink the STATIC IP address from the interface."""
        # If the allocation type is only changing then we don't need to delete
        # the IP address it needs to be updated.
        if swap_alloc_type is not None:
            static_ip.alloc_type = swap_alloc_type
            static_ip.ip = None
            static_ip.save()
        else:
            static_ip.delete()
        return static_ip

    def unlink_ip_address(
            self, ip_address, clearing_config=False):
        """Remove the `IPAddress` link on interface.

        :param clearing_config: Set to True when the entire network
            configuration for this interface is being cleared. This makes sure
            that the auto created link_up is not created.
        """
        mode = ip_address.get_interface_link_type()
        if mode == INTERFACE_LINK_TYPE.STATIC:
            self._unlink_static_ip(ip_address)
        else:
            ip_address.delete()
        # Always ensure that an interface that is enabled without any links
        # gets a LINK_UP link.
        if self.enabled and not clearing_config:
            self.ensure_link_up()

    def unlink_subnet_by_id(self, link_id):
        """Remove the `IPAddress` link on interface by its ID."""
        ip_address = self.ip_addresses.get(id=link_id)
        self.unlink_ip_address(ip_address)

    def _swap_subnet(self, static_ip, subnet, ip_address=None):
        """Swap the subnet for the `static_ip`."""
        # Check that requested `ip_address` is available.
        if ip_address is not None:
            already_used = get_one(
                StaticIPAddress.objects.filter(ip=ip_address))
            if already_used is not None:
                raise StaticIPAddressUnavailable(
                    "IP address is already in use.")

        # Link to the new subnet.
        return self._link_subnet_static(
            subnet, ip_address=ip_address, swap_static_ip=static_ip)

    def update_ip_address(
            self, static_ip, mode, subnet, ip_address=None):
        """Update an already existing link on interface to be the new data."""
        if mode == INTERFACE_LINK_TYPE.AUTO:
            new_alloc_type = IPADDRESS_TYPE.AUTO
        elif mode == INTERFACE_LINK_TYPE.DHCP:
            new_alloc_type = IPADDRESS_TYPE.DHCP
        elif mode in [INTERFACE_LINK_TYPE.LINK_UP, INTERFACE_LINK_TYPE.STATIC]:
            new_alloc_type = IPADDRESS_TYPE.STICKY

        current_mode = static_ip.get_interface_link_type()
        if current_mode == INTERFACE_LINK_TYPE.STATIC:
            if mode == INTERFACE_LINK_TYPE.STATIC:
                if (static_ip.subnet == subnet and (
                        ip_address is None or static_ip.ip == ip_address)):
                    # Same subnet and IP address nothing to do.
                    return static_ip
                # Update the subnet and IP address for the static assignment.
                return self._swap_subnet(
                    static_ip, subnet, ip_address=ip_address)
            else:
                # Not staying in the same mode so we can just remove the
                # static IP and change its alloc_type from STICKY.
                static_ip = self._unlink_static_ip(
                    static_ip, swap_alloc_type=new_alloc_type)
        elif mode == INTERFACE_LINK_TYPE.STATIC:
            # Linking to the subnet statically were the original was not a
            # static link. Swap the objects so the object keeps the same ID.
            return self._link_subnet_static(
                subnet, ip_address=ip_address,
                swap_static_ip=static_ip)
        static_ip.alloc_type = new_alloc_type
        static_ip.ip = None
        static_ip.subnet = subnet
        static_ip.save()
        return static_ip

    def update_link_by_id(self, link_id, mode, subnet, ip_address=None):
        """Update the `IPAddress` link on interface by its ID."""
        static_ip = self.ip_addresses.get(id=link_id)
        return self.update_ip_address(
            static_ip, mode, subnet, ip_address=ip_address)

    def clear_all_links(self, clearing_config=False):
        """Remove all the `IPAddress` link on the interface."""
        for ip_address in self.ip_addresses.exclude(
                alloc_type=IPADDRESS_TYPE.DISCOVERED):
            self.unlink_ip_address(ip_address, clearing_config=clearing_config)

    def claim_auto_ips(self, exclude_addresses=[]):
        """Claim IP addresses for this interfaces AUTO IP addresses.

        :param exclude_addresses: Exclude the following IP addresses in the
            allocation. Mainly used to ensure that the sub-transaction that
            runs to identify available IP address does not include the already
            allocated IP addresses.
        """
        exclude_addresses = set(exclude_addresses)
        assigned_addresses = []
        for auto_ip in self.ip_addresses.filter(
                alloc_type=IPADDRESS_TYPE.AUTO):
            if not auto_ip.ip:
                assigned_ip = self._claim_auto_ip(
                    auto_ip, exclude_addresses=exclude_addresses)
                if assigned_ip is not None:
                    assigned_addresses.append(assigned_ip)
                    exclude_addresses.add(str(assigned_ip.ip))
        return assigned_addresses

    def _claim_auto_ip(self, auto_ip, exclude_addresses=[]):
        """Claim an IP address for the `auto_ip`."""
        subnet = auto_ip.subnet
        if subnet is None:
            maaslog.error(
                "Could not find subnet for interface %s." %
                (self.get_log_string()))
            raise StaticIPAddressUnavailable(
                "Automatic IP address cannot be configured on interface %s "
                "without an associated subnet." % self.get_name())

        # Allocate a new IP address from the entire subnet, excluding already
        # allocated addresses and ranges.
        new_ip = StaticIPAddress.objects.allocate_new(
            subnet=subnet, alloc_type=IPADDRESS_TYPE.AUTO,
            exclude_addresses=exclude_addresses)
        self.ip_addresses.add(new_ip)
        maaslog.info("Allocated automatic IP address %s for %s." % (
            new_ip.ip,
            self.get_log_string()))

        auto_ip.delete()
        return new_ip

    def release_auto_ips(self):
        """Release all AUTO IP address for this interface that have an IP
        address assigned."""
        affected_subnets = set()
        released_addresses = []
        for auto_ip in self.ip_addresses.filter(
                alloc_type=IPADDRESS_TYPE.AUTO):
            if auto_ip.ip:
                subnet, released_ip = self._release_auto_ip(auto_ip)
                if subnet is not None:
                    affected_subnets.add(subnet)
                released_addresses.append(released_ip)
        return released_addresses

    def _release_auto_ip(self, auto_ip):
        """Release the IP address assigned to the `auto_ip`."""
        subnet = auto_ip.subnet
        auto_ip.ip = None
        auto_ip.save()
        return subnet, auto_ip

    def create_acquired_bridge(self, bridge_stp=None, bridge_fd=None):
        """Create an acquired bridge on top of this interface.

        Cannot be called on a `BridgeInterface`.
        """
        if bridge_stp is None:
            bridge_stp = False
        if bridge_fd is None:
            bridge_fd = DEFAULT_BRIDGE_FD
        if self.type == INTERFACE_TYPE.BRIDGE:
            raise ValueError(
                "Cannot create an acquired bridge on a bridge interface.")
        params = {
            'bridge_stp': bridge_stp,
            'bridge_fd': bridge_fd,
        }
        if 'mtu' in self.params:
            params['mtu'] = self.params['mtu']
        bridge = BridgeInterface(
            name="br-%s" % self.get_name(), node=self.node,
            mac_address=self.mac_address, vlan=self.vlan,
            enabled=True, acquired=True, params=params)
        bridge.save()
        # The order in which the creating and linkage between child and parent
        # is important. The IP addresses must first be moved from this
        # interface to the created bridge before the parent can be set on the
        # bridge.
        for sip in self.ip_addresses.all():
            if sip.alloc_type != IPADDRESS_TYPE.DISCOVERED:
                bridge.ip_addresses.add(sip)
                self.ip_addresses.remove(sip)
        InterfaceRelationship(child=bridge, parent=self).save()
        return bridge

    def get_ancestors(self):
        """Returns all the ancestors of the interface (that is, including each
        parent's parents, and so on.)
        """
        parents = set(rel.parent for rel in self.parent_relationships.all())
        parent_relationships = set(self.parent_relationships.all())
        for parent_rel in parent_relationships:
            parents |= parent_rel.parent.get_ancestors()
        return parents

    def get_successors(self):
        """Returns all the ancestors of the interface (that is, including each
        child's children, and so on.)
        """
        children = set(rel.child for rel in self.children_relationships.all())
        children_relationships = set(self.children_relationships.all())
        for child_rel in children_relationships:
            children |= child_rel.child.get_successors()
        return children

    def get_all_related_interfaces(self):
        """Returns all of the related interfaces (including any ancestors,
        successors, and ancestors' successors)."""
        ancestors = self.get_ancestors()
        all_related = set()
        all_related |= ancestors
        for ancestor in ancestors:
            all_related |= ancestor.get_successors()
        return all_related

    def clean(self):
        super(Interface, self).clean()
        # Acquired can only be set on bridge interface types and the node
        # must be in an allocated state.
        if self.acquired and self.type != INTERFACE_TYPE.BRIDGE:
            raise ValueError(
                "acquired cannot be True on interface type '%s'" % self.type)

    def delete(self, remove_ip_address=True):
        # We set the _skip_ip_address_removal so the signal can use it to
        # skip removing the IP addresses. This is normally only done by the
        # lease parser, because it will delete UnknownInterface's when the
        # lease goes away. We don't need to tell the cluster to remove the
        # lease then.
        if not remove_ip_address:
            self._skip_ip_address_removal = True
        super(Interface, self).delete()

    def add_tag(self, tag):
        """Add tag to interface."""
        if tag not in self.tags:
            self.tags.append(tag)

    def remove_tag(self, tag):
        """Remove tag from interface."""
        if tag in self.tags:
            self.tags.remove(tag)


class InterfaceRelationship(CleanSave, TimestampedModel):
    child = ForeignKey(Interface, related_name="parent_relationships")
    parent = ForeignKey(Interface, related_name="children_relationships")


class PhysicalInterface(Interface):

    class Meta(Interface.Meta):
        proxy = True
        verbose_name = "Physical interface"
        verbose_name_plural = "Physical interface"

    @classmethod
    def get_type(self):
        return INTERFACE_TYPE.PHYSICAL

    def clean(self):
        super(PhysicalInterface, self).clean()
        # Node and MAC address is always required for a physical interface.
        validation_errors = {}
        if self.node is None:
            validation_errors["node"] = ["This field cannot be blank."]
        if self.mac_address is None:
            validation_errors["mac_address"] = ["This field cannot be blank."]
        if len(validation_errors) > 0:
            raise ValidationError(validation_errors)

        # MAC address must be unique amongst every PhysicalInterface.
        other_interfaces = PhysicalInterface.objects.filter(
            mac_address=self.mac_address)
        if self.id is not None:
            other_interfaces = other_interfaces.exclude(id=self.id)
        other_interfaces = other_interfaces.all()
        if len(other_interfaces) > 0:
            raise ValidationError({
                "mac_address": [
                    "This MAC address is already in use by %s." % (
                        other_interfaces[0].get_log_string())]
            })

        # No parents are allow for a physical interface.
        if self.id is not None:
            # Use the precache so less queries are made.
            if len(self.parents.all()) > 0:
                raise ValidationError({
                    "parents": ["A physical interface cannot have parents."]
                })


class ChildInterface(Interface):
    """Abstract class to represent interfaces which require parents in order
    to operate.
    """

    class Meta(Interface.Meta):
        proxy = True

    def get_node(self):
        if self.id is None:
            # This should be correct since the interface was just now created.
            return self.node
        else:
            parent = self.parents.first()
            if parent is not None and parent != self:
                return parent.get_node()
            else:
                # This could be a bridge interface without a parent.
                return self.node

    def is_enabled(self):
        if self.id is None:
            return True
        else:
            if self.parents.count() > 0:
                is_enabled = {
                    parent.is_enabled()
                    for parent in self.parents.all()
                    if parent != self
                }
                return True in is_enabled
            else:
                return self.enabled

    def _validate_acceptable_parent_types(self, parent_types):
        """Raises a ValidationError if the interface has parents which are not
        allowed, given this interface type. (for example, only physical
        interfaces can be bonded, and bridges cannot bridge other bridges.)
        """
        raise NotImplementedError()

    def _validate_parent_interfaces(self):
        # Parent interfaces on this bond must be from the same node and can
        # only be physical interfaces.
        if self.id is not None:
            nodes = {
                parent.node
                for parent in self.parents.all()
            }
            if len(nodes) > 1:
                raise ValidationError({
                    "parents": [
                        "Parent interfaces do not belong to the same node."]
                })
            parent_types = {
                parent.get_type()
                for parent in self.parents.all()
            }
            self._validate_acceptable_parent_types(parent_types)

    def _validate_unique_or_parent_mac(self):
        # Validate that this bond interface is using either a new MAC address
        # or a MAC address from one of its parents. This validation is only
        # done once the interface has been saved once. That is because if its
        # done before it would always fail. As the validation would see that
        # its soon to be parents MAC address is already in use.
        if self.id is not None:
            interfaces = Interface.objects.filter(mac_address=self.mac_address)
            related_ids = [
                parent.id
                for parent in self.get_all_related_interfaces()
            ]
            bad_interfaces = []
            for interface in interfaces:
                if self.id == interface.id:
                    # Self in database so ignore.
                    continue
                elif interface.id in related_ids:
                    # Found the same MAC on either a parent, a child, or
                    # another of the parents' children.
                    continue
                else:
                    # Its not unique and its not a parent interface if we
                    # made it this far.
                    bad_interfaces.append(interface)
            if len(bad_interfaces) > 0:
                maaslog.warning(
                    "While adding %s: "
                    "found a MAC address already in use by %s." % (
                        self.get_log_string(),
                        bad_interfaces[0].get_log_string()))


class BridgeInterface(ChildInterface):

    class Meta(Interface.Meta):
        proxy = True
        verbose_name = "Bridge"
        verbose_name_plural = "Bridges"

    @classmethod
    def get_type(self):
        return INTERFACE_TYPE.BRIDGE

    def _validate_acceptable_parent_types(self, parent_types):
        """Validates that bridges cannot contain other bridges."""
        if INTERFACE_TYPE.BRIDGE in parent_types:
            raise ValidationError({
                "parents": ["Bridges cannot contain other bridges."]
            })

    def clean(self):
        super().clean()
        # Validate that the MAC address is not None.
        if not self.mac_address:
            raise ValidationError({
                "mac_address": ["This field cannot be blank."]
            })
        self._validate_parent_interfaces()
        self._validate_unique_or_parent_mac()

    def save(self, *args, **kwargs):
        # Set the node of this bond to the same as its parents.
        self.node = self.get_node()
        # Set the enabled status based on its parents.
        self.enabled = self.is_enabled()
        super().save(*args, **kwargs)


class BondInterface(ChildInterface):

    class Meta(Interface.Meta):
        proxy = True
        verbose_name = "Bond"
        verbose_name_plural = "Bonds"

    @classmethod
    def get_type(self):
        return INTERFACE_TYPE.BOND

    def clean(self):
        super(BondInterface, self).clean()
        # Validate that the MAC address is not None.
        if not self.mac_address:
            raise ValidationError({
                "mac_address": ["This field cannot be blank."]
            })

        self._validate_parent_interfaces()
        self._validate_unique_or_parent_mac()

    def _validate_acceptable_parent_types(self, parent_types):
        """Validates that bonds only include physical interfaces."""
        if parent_types != {INTERFACE_TYPE.PHYSICAL}:
            raise ValidationError({
                "parents": ["Only physical interfaces can be bonded."]
            })

    def save(self, *args, **kwargs):
        # Set the node of this bond to the same as its parents.
        self.node = self.get_node()
        # Set the enabled status based on its parents.
        self.enabled = self.is_enabled()
        super(BondInterface, self).save(*args, **kwargs)


def build_vlan_interface_name(parent, vlan):
    if parent:
        return "%s.%d" % (parent.get_name(), vlan.vid)
    else:
        return "unknown.%d" % vlan.vid


class VLANInterface(Interface):

    class Meta(Interface.Meta):
        proxy = True
        verbose_name = "VLAN interface"
        verbose_name_plural = "VLAN interfaces"

    @classmethod
    def get_type(self):
        return INTERFACE_TYPE.VLAN

    def get_node(self):
        if self.id is None:
            return None
        else:
            parent = self.parents.first()
            if parent is not None:
                return parent.get_node()
            else:
                return None

    def is_enabled(self):
        if self.id is None:
            return True
        else:
            parent = self.parents.first()
            if parent is not None:
                return parent.is_enabled()
            else:
                return True

    def get_name(self):
        if self.id is not None:
            parent = self.parents.first()
            if parent is not None:
                return build_vlan_interface_name(parent, self.vlan)
        # self.vlan is always something valid.
        return "vlan%d" % self.vlan.vid

    def clean(self):
        super(VLANInterface, self).clean()
        if self.id is not None:
            # Use the precache here instead of the count() method.
            parents = self.parents.all()
            parent_count = len(parents)
            if parent_count == 0 or parent_count > 1:
                raise ValidationError({
                    "parents": ["VLAN interface must have exactly one parent."]
                    })
            parent = parents[0]
            # We do not allow a bridge interface to be a parent for a VLAN
            # interface.
            allowed_vlan_parent_types = (
                INTERFACE_TYPE.PHYSICAL,
                INTERFACE_TYPE.BOND,
                INTERFACE_TYPE.BRIDGE
            )
            if parent.get_type() not in allowed_vlan_parent_types:
                # XXX blake_r 2016-07-18: we won't mention bridges in this
                # error message, since users can't configure VLAN interfaces
                # on bridges.
                raise ValidationError({
                    "parents": [
                        "VLAN interface can only be created on a physical "
                        "or bond interface."
                    ]
                })
            # VLAN interface must be connected to a VLAN, it cannot be
            # disconnected like physical and bond interfaces.
            if self.vlan is None:
                raise ValidationError({
                    "vlan": [
                        "VLAN interface requires connection to a VLAN."
                    ]
                })

    def save(self, *args, **kwargs):
        # Set the node of this VLAN to the same as its parents.
        self.node = self.get_node()

        # Set the enabled status based on its parents.
        self.enabled = self.is_enabled()

        # Set the MAC address to the same as its parent.
        if self.id is not None:
            parent = self.parents.first()
            if parent is not None:
                self.mac_address = parent.mac_address

        # Auto update the interface name.
        new_name = self.get_name()
        if self.name != new_name:
            self.name = new_name
        return super(VLANInterface, self).save(*args, **kwargs)


class UnknownInterface(Interface):

    class Meta(Interface.Meta):
        proxy = True
        verbose_name = "Unknown interface"
        verbose_name_plural = "Unknown interfaces"

    @classmethod
    def get_type(self):
        return INTERFACE_TYPE.UNKNOWN

    def get_node(self):
        return None

    def clean(self):
        super(UnknownInterface, self).clean()
        if self.node is not None:
            raise ValidationError({
                "node": ["This field must be blank."]
            })

        # No other interfaces can have this MAC address.
        other_interfaces = Interface.objects.filter(
            mac_address=self.mac_address)
        if self.id is not None:
            other_interfaces = other_interfaces.exclude(id=self.id)
        other_interfaces = other_interfaces.all()
        if len(other_interfaces) > 0:
            maaslog.warning(
                "While adding %s: "
                "found a MAC address already in use by %s." % (
                    self.get_log_string(),
                    other_interfaces[0].get_log_string()))

        # Cannot have any parents.
        if self.id is not None:
            # Use the precache here instead of the count() method.
            parents = self.parents.all()
            if len(parents) > 0:
                raise ValidationError({
                    "parents": ["A unknown interface cannot have parents."]
                })


INTERFACE_TYPE_MAPPING = {
    klass.get_type(): klass
    for klass in
    [
        PhysicalInterface,
        BondInterface,
        BridgeInterface,
        VLANInterface,
        UnknownInterface,
    ]
}

ALL_INTERFACE_TYPES = set(INTERFACE_TYPE_MAPPING.values())
