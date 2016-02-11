# Copyright 2012-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

__all__ = [
    "AnonNodesHandler",
    "NodeHandler",
    "NodesHandler",
    "store_node_power_parameters",
]

import json

import bson
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from maasserver.api.logger import maaslog
from maasserver.api.support import (
    admin_method,
    AnonymousOperationsHandler,
    operation,
    OperationsHandler,
)
from maasserver.api.utils import (
    get_mandatory_param,
    get_optional_list,
    get_optional_param,
)
from maasserver.clusterrpc.power_parameters import get_power_types
from maasserver.enum import (
    NODE_PERMISSION,
    NODE_STATUS,
)
from maasserver.exceptions import (
    MAASAPIBadRequest,
    MAASAPIValidationError,
)
from maasserver.fields import MAC_RE
from maasserver.forms import BulkNodeActionForm
from maasserver.models import (
    Interface,
    Node,
)
from maasserver.models.nodeprobeddetails import get_single_probed_details
from piston3.utils import rc
from provisioningserver.power.schema import UNKNOWN_POWER_TYPE

# Node's fields exposed on the API.
DISPLAYED_NODE_FIELDS = (
    'system_id',
    'hostname',
    'owner',
    'macaddress_set',
    'architecture',
    'min_hwe_kernel',
    'hwe_kernel',
    'cpu_count',
    'memory',
    'swap_size',
    'storage',
    'status',
    'osystem',
    'distro_series',
    'netboot',
    'power_type',
    'power_state',
    'tag_names',
    'address_ttl',
    'ip_addresses',
    ('interface_set', (
        'id',
        'name',
        'type',
        'vlan',
        'mac_address',
        'parents',
        'children',
        'tags',
        'enabled',
        'links',
        'params',
        'discovered',
        'effective_mtu',
        )),
    'zone',
    'disable_ipv4',
    'constraint_map',
    'constraints_by_type',
    'boot_disk',
    'blockdevice_set',
    'physicalblockdevice_set',
    'virtualblockdevice_set',
    'substatus_action',
    'substatus_message',
    'substatus_name',
    'node_type',
)


def store_node_power_parameters(node, request):
    """Store power parameters in request.

    The parameters should be JSON, passed with key `power_parameters`.
    """
    power_type = request.POST.get("power_type", None)
    if power_type is None:
        return

    # TODO: Pass controller list to restrict to node-available power_types.
    power_types = get_power_types(None)

    if power_type in power_types or power_type == UNKNOWN_POWER_TYPE:
        node.power_type = power_type
    else:
        raise MAASAPIBadRequest("Bad power_type '%s'" % power_type)

    power_parameters = request.POST.get("power_parameters", None)
    if power_parameters and not power_parameters.isspace():
        try:
            node.power_parameters = json.loads(power_parameters)
        except ValueError:
            raise MAASAPIBadRequest("Failed to parse JSON power_parameters")

    node.save()


def filtered_nodes_list_from_request(request, model=None):
    """List Nodes visible to the user, optionally filtered by criteria.

    Nodes are sorted by id (i.e. most recent last).

    :param hostname: An optional hostname. Only events relating to the node
        with the matching hostname will be returned. This can be specified
        multiple times to get events relating to more than one node.
    :param mac_address: An optional MAC address. Only events relating to the
        node owning the specified MAC address will be returned. This can be
        specified multiple times to get events relating to more than one node.
    :param id: An optional list of system ids.  Only events relating to the
        nodes with matching system ids will be returned.
    :param domain: An optional name for a dns domain. Only events relating to
        the nodes in the domain will be returned.
    :param zone: An optional name for a physical zone. Only events relating to
        the nodes in the zone will be returned.
    :param agent_name: An optional agent name.  Only events relating to the
        nodes with matching agent names will be returned.
    """
    # Get filters from request.
    match_ids = get_optional_list(request.GET, 'id')

    match_macs = get_optional_list(request.GET, 'mac_address')
    if match_macs is not None:
        invalid_macs = [
            mac for mac in match_macs if MAC_RE.match(mac) is None]
        if len(invalid_macs) != 0:
            raise MAASAPIValidationError(
                "Invalid MAC address(es): %s" % ", ".join(invalid_macs))

    if model is None:
        model = Node
    # Fetch nodes and apply filters.
    nodes = model.objects.get_nodes(
        request.user, NODE_PERMISSION.VIEW, ids=match_ids)
    if match_macs is not None:
        nodes = nodes.filter(interface__mac_address__in=match_macs)
    match_hostnames = get_optional_list(request.GET, 'hostname')
    if match_hostnames is not None:
        nodes = nodes.filter(hostname__in=match_hostnames)
    match_domains = get_optional_list(request.GET, 'domain')
    if match_domains is not None:
        nodes = nodes.filter(domain__name__in=match_domains)
    match_zone_name = request.GET.get('zone', None)
    if match_zone_name is not None:
        nodes = nodes.filter(zone__name=match_zone_name)
    match_agent_name = request.GET.get('agent_name', None)
    if match_agent_name is not None:
        nodes = nodes.filter(agent_name=match_agent_name)

    return nodes.order_by('id')


class NodeHandler(OperationsHandler):
    """Manage an individual Node.

    The Node is identified by its system_id.
    """
    api_doc_section_name = "Node"

    create = None  # Disable create.
    model = Node
    fields = DISPLAYED_NODE_FIELDS

    # Override the 'hostname' field so that it returns the FQDN instead as
    # this is used by Juju to reach that node.
    @classmethod
    def hostname(handler, node):
        return node.fqdn

    # Override 'owner' so it emits the owner's name rather than a
    # full nested user object.
    @classmethod
    def owner(handler, node):
        if node.owner is None:
            return None
        return node.owner.username

    @classmethod
    def macaddress_set(handler, node):
        return [
            {"mac_address": "%s" % interface.mac_address}
            for interface in node.interface_set.all()
            if interface.mac_address
        ]

    def read(self, request, system_id):
        """Read a specific Node.

        Returns 404 if the node is not found.
        """
        return self.model.objects.get_node_or_404(
            system_id=system_id, user=request.user, perm=NODE_PERMISSION.VIEW)

    def delete(self, request, system_id):
        """Delete a specific Node.

        Returns 404 if the node is not found.
        Returns 403 if the user does not have permission to delete the node.
        Returns 204 if the node is successfully deleted.
        """
        node = self.model.objects.get_node_or_404(
            system_id=system_id, user=request.user,
            perm=NODE_PERMISSION.ADMIN)
        node.delete()
        return rc.DELETED

    @classmethod
    def resource_uri(cls, node=None):
        # This method is called by piston in two different contexts:
        # - when generating an uri template to be used in the documentation
        # (in this case, it is called with node=None).
        # - when populating the 'resource_uri' field of an object
        # returned by the API (in this case, node is a Node object).
        node_system_id = "system_id"
        if node is not None:
            node_system_id = node.system_id
        return ('node_handler', (node_system_id, ))

    @operation(idempotent=True)
    def details(self, request, system_id):
        """Obtain various system details.

        For example, LLDP and ``lshw`` XML dumps.

        Returns a ``{detail_type: xml, ...}`` map, where
        ``detail_type`` is something like "lldp" or "lshw".

        Note that this is returned as BSON and not JSON. This is for
        efficiency, but mainly because JSON can't do binary content
        without applying additional encoding like base-64.

        Returns 404 if the node is not found.
        """
        node = get_object_or_404(self.model, system_id=system_id)
        probe_details = get_single_probed_details(node.system_id)
        probe_details_report = {
            name: None if data is None else bson.Binary(data)
            for name, data in probe_details.items()
        }
        return HttpResponse(
            bson.BSON.encode(probe_details_report),
            # Not sure what media type to use here.
            content_type='application/bson')

    @operation(idempotent=False)
    def mark_broken(self, request, system_id):
        """Mark a node as 'broken'.

        If the node is allocated, release it first.

        :param comment: Optional comment for the event log. Will be
            displayed on the Node as an error description until marked fixed.
        :type comment: unicode

        Returns 404 if the node is not found.
        Returns 403 if the user does not have permission to mark the node
        broken.
        """
        node = self.model.objects.get_node_or_404(
            user=request.user, system_id=system_id, perm=NODE_PERMISSION.EDIT)
        comment = get_optional_param(request.POST, 'comment')
        if not comment:
            # read old error_description to for backward compatibility
            comment = get_optional_param(request.POST, 'error_description')
        node.mark_broken(request.user, comment)
        return node

    @operation(idempotent=False)
    def mark_fixed(self, request, system_id):
        """Mark a broken node as fixed and set its status as 'ready'.

        :param comment: Optional comment for the event log.
        :type comment: unicode

        Returns 404 if the node is not found.
        Returns 403 if the user does not have permission to mark the node
        fixed.
        """
        comment = get_optional_param(request.POST, 'comment')
        node = self.model.objects.get_node_or_404(
            user=request.user, system_id=system_id, perm=NODE_PERMISSION.ADMIN)
        node.mark_fixed(request.user, comment)
        maaslog.info(
            "%s: User %s marked node as fixed", node.hostname,
            request.user.username)
        return node


class AnonNodesHandler(AnonymousOperationsHandler):
    """Anonymous access to Nodes."""
    create = update = delete = None
    model = Node
    fields = DISPLAYED_NODE_FIELDS

    # Override the 'hostname' field so that it returns the FQDN instead as
    # this is used by Juju to reach that node.
    @classmethod
    def hostname(handler, node):
        return node.fqdn

    @operation(idempotent=True)
    def is_registered(self, request):
        """Returns whether or not the given MAC address is registered within
        this MAAS (and attached to a non-retired node).

        :param mac_address: The mac address to be checked.
        :type mac_address: unicode
        :return: 'true' or 'false'.
        :rtype: unicode

        Returns 400 if any mandatory parameters are missing.
        """
        mac_address = get_mandatory_param(request.GET, 'mac_address')
        interfaces = Interface.objects.filter(mac_address=mac_address)
        interfaces = interfaces.exclude(node__status=NODE_STATUS.RETIRED)
        return interfaces.exists()

    @classmethod
    def resource_uri(cls, *args, **kwargs):
        return ('nodes_handler', [])


class NodesHandler(OperationsHandler):
    """Manage the collection of all the nodes in the MAAS."""
    api_doc_section_name = "Nodes"
    create = update = delete = None
    anonymous = AnonNodesHandler
    base_model = Node

    def read(self, request):
        """List all nodes."""
        nodes = filtered_nodes_list_from_request(request, self.base_model)

        # Prefetch related objects that are needed for rendering the result.
        nodes = nodes.prefetch_related('interface_set__node')
        nodes = nodes.prefetch_related(
            'interface_set__ip_addresses')
        nodes = nodes.prefetch_related('tags')
        nodes = nodes.prefetch_related('zone')
        return nodes.order_by('id')

    @admin_method
    @operation(idempotent=False)
    def set_zone(self, request):
        """Assign multiple nodes to a physical zone at once.

        :param zone: Zone name.  If omitted, the zone is "none" and the nodes
            will be taken out of their physical zones.
        :param nodes: system_ids of the nodes whose zones are to be set.
           (An empty list is acceptable).

        Raises 403 if the user is not an admin.
        """
        data = {
            'action': 'set_zone',
            'zone': request.data.get('zone'),
            'system_id': get_optional_list(request.data, 'nodes'),
        }
        form = BulkNodeActionForm(request.user, data=data)
        if not form.is_valid():
            raise MAASAPIValidationError(form.errors)
        form.save()

    @classmethod
    def resource_uri(cls, *args, **kwargs):
        return ('nodes_handler', [])
