# Copyright (C) 2011-2014 Red Hat, Inc.
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301
# USA
#
# Author: tasleson
#         Gris Ge <fge@redhat.com>

from string import split
import time
import traceback

import pywbem
from pywbem import CIMError

from lsm import (IStorageAreaNetwork, error, uri_parse, LsmError, ErrorNumber,
                 JobStatus, md5, Pool, Volume, AccessGroup, System,
                 Capabilities, Disk, txt_a, VERSION, TargetPort,
                 search_property)

## Variable Naming scheme:
#   cim_xxx         CIMInstance
#   cim_xxx_path    CIMInstanceName
#   cim_sys         CIM_ComputerSystem  (root or leaf)
#   cim_pool        CIM_StoragePool
#   cim_scs         CIM_StorageConfigurationService
#   cim_vol         CIM_StorageVolume
#   cim_rp          CIM_RegisteredProfile
#   cim_init        CIM_StorageHardwareID
#   cim_ag          CIM_SCSIProtocolController
#   cim_fc_tgt      CIM_FCPort
#   cim_iscsi_pg    CIM_iSCSIProtocolEndpoint   # iSCSI portal group
#   cim_iscsi_node  CIM_SCSIProtocolController
#   cim_tcp         CIM_TCPProtocolEndpoint,
#   cim_ip          CIM_IPProtocolEndpoint
#   cim_eth         CIM_EthernetPort
#
#   sys             Object of LSM System
#   pool            Object of LSM Pool
#   vol             Object of LSM Volume

## Method Naming schme:
#   _cim_xxx()
#       Return CIMInstance without any Associators() call.
#   _cim_xxx_of(cim_yyy)
#       Return CIMInstance associated to cim_yyy
#   _adj_cim_xxx()
#       Retrun CIMInstance with 'adj' only
#   _cim_xxx_of_id(some_id)
#       Return CIMInstance for given ID


def handle_cim_errors(method):
    def cim_wrapper(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except LsmError as lsm:
            raise
        except CIMError as ce:
            error_code, desc = ce

            if error_code == 0 and 'Socket error' in desc:
                if 'Errno 111' in desc:
                    raise LsmError(ErrorNumber.NETWORK_CONNREFUSED,
                                   'Connection refused')
                if 'Errno 113' in desc:
                    raise LsmError(ErrorNumber.NETWORK_HOSTDOWN,
                                   'Host is down')
            raise LsmError(ErrorNumber.LSM_BUG, desc)
        except pywbem.cim_http.AuthError as ae:
            raise LsmError(ErrorNumber.PLUGIN_AUTH_FAILED, "Unauthorized user")
        except pywbem.cim_http.Error as te:
            raise LsmError(ErrorNumber.NETWORK_ERROR, str(te))
        except Exception as e:
            error("Unexpected exception:\n" + traceback.format_exc())
            raise LsmError(ErrorNumber.PLUGIN_ERROR, str(e),
                           traceback.format_exc())
    return cim_wrapper


def _spec_ver_str_to_num(spec_ver_str):
    """
    Convert version string stored in CIM_RegisteredProfile to a integer.
    Example:
        "1.5.1" -> 1,005,001
    """
    tmp_list = [0, 0, 0]
    tmp_list = spec_ver_str.split(".")
    if len(tmp_list) == 2:
        tmp_list.extend([0])
    if len(tmp_list) == 3:
        return (int(tmp_list[0]) * 10 ** 6 +
                int(tmp_list[1]) * 10 ** 3 +
                int(tmp_list[2]))
    return None


def _merge_list(list_a, list_b):
    return list(set(list_a + list_b))


def _hex_string_format(hex_str, length, every):
    hex_str = hex_str.lower()
    return ':'.join(hex_str[i:i + every] for i in range(0, length, every))


class DMTF(object):
    # CIM_StorageHardwareID['IDType']
    ID_TYPE_OTHER = pywbem.Uint16(1)
    ID_TYPE_WWPN = pywbem.Uint16(2)
    ID_TYPE_WWNN = pywbem.Uint16(3)
    ID_TYPE_HOSTNAME = pywbem.Uint16(4)
    ID_TYPE_ISCSI = pywbem.Uint16(5)
    ID_TYPE_SW_WWN = pywbem.Uint16(6)
    ID_TYPE_SAS = pywbem.Uint16(7)
    TGT_PORT_USAGE_FRONTEND_ONLY = pywbem.Uint16(2)
    TGT_PORT_USAGE_UNRESTRICTED = pywbem.Uint16(4)
    # CIM_FCPort['PortDiscriminator']
    FC_PORT_PORT_DISCRIMINATOR_FCOE = pywbem.Uint16(10)
    # CIM_NetworkPort['LinkTechnology']
    NET_PORT_LINK_TECH_ETHERNET = pywbem.Uint16(2)
    # CIM_iSCSIProtocolEndpoint['Role']
    ISCSI_TGT_ROLE_TARGET = pywbem.Uint16(3)
    # CIM_SCSIProtocolController['NameFormat']
    SPC_NAME_FORMAT_ISCSI = pywbem.Uint16(3)
    # CIM_IPProtocolEndpoint['IPv6AddressType']
    IPV6_ADDR_TYPE_GUA = pywbem.Uint16(6)
    # GUA: Global Unicast Address.
    #      2000::/3
    IPV6_ADDR_TYPE_6TO4 = pywbem.Uint16(7)
    # IPv6 to IPv4 transition
    #      ::ffff:0:0/96
    #      ::ffff:0:0:0/96
    #      64:ff9b::/96     # well-known prefix
    #      2002::/16        # 6to4
    IPV6_ADDR_TYPE_ULA = pywbem.Uint16(8)
    # ULA: Unique Local Address, aka Site Local Unicast.
    #      fc00::/7


_INIT_TYPE_CONV = {
    DMTF.ID_TYPE_OTHER: AccessGroup.INIT_TYPE_OTHER,
    DMTF.ID_TYPE_WWPN: AccessGroup.INIT_TYPE_WWPN,
    DMTF.ID_TYPE_WWNN: AccessGroup.INIT_TYPE_WWNN,
    DMTF.ID_TYPE_HOSTNAME: AccessGroup.INIT_TYPE_HOSTNAME,
    DMTF.ID_TYPE_ISCSI: AccessGroup.INIT_TYPE_ISCSI_IQN,
    DMTF.ID_TYPE_SW_WWN: AccessGroup.INIT_TYPE_OTHER,
    DMTF.ID_TYPE_SAS: AccessGroup.INIT_TYPE_SAS,
}


def _dmtf_init_type_to_lsm(cim_init):
    if 'IDType' in cim_init and cim_init['IDType'] in _INIT_TYPE_CONV.keys():
        return _INIT_TYPE_CONV[cim_init['IDType']]
    return AccessGroup.INIT_TYPE_UNKNOWN


def _get_key(dictionary, value):
    keys = [k for k, v in dictionary.items() if v == value]
    if len(keys) > 0:
        return keys[0]
    return None


def _lsm_tgt_port_type_of_cim_fc_tgt(cim_fc_tgt):
    """
    We are assuming we got CIM_FCPort. Caller should make sure of that.
    Return TargetPool.PORT_TYPE_FC as fallback
    """
    # In SNIA SMI-S 1.6.1 public draft 2, 'PortDiscriminator' is mandatroy
    # for FCoE target port.
    if 'PortDiscriminator' in cim_fc_tgt and \
       cim_fc_tgt['PortDiscriminator'] and \
       DMTF.FC_PORT_PORT_DISCRIMINATOR_FCOE in cim_fc_tgt['PortDiscriminator']:
        return TargetPort.PORT_TYPE_FCOE
    if 'LinkTechnology' in cim_fc_tgt and \
       cim_fc_tgt['LinkTechnology'] == DMTF.NET_PORT_LINK_TECH_ETHERNET:
        return TargetPort.PORT_TYPE_FCOE
    return TargetPort.PORT_TYPE_FC


def _lsm_init_type_to_dmtf(init_type):
    key = _get_key(_INIT_TYPE_CONV, init_type)
    if key is None:
        raise LsmError(ErrorNumber.NO_SUPPORT,
                       "Does not support provided init_type: %d" % init_type)
    else:
        return key


class SNIA(object):
    BLK_ROOT_PROFILE = 'Array'
    BLK_SRVS_PROFILE = 'Block Services'
    DISK_LITE_PROFILE = 'Disk Drive Lite'
    MULTI_SYS_PROFILE = 'Multiple Computer System'
    MASK_PROFILE = 'Masking and Mapping'
    FC_TGT_PORT_PROFILE = 'FC Target Ports'
    ISCSI_TGT_PORT_PROFILE = 'iSCSI Target Ports'
    SMIS_SPEC_VER_1_4 = '1.4'
    SMIS_SPEC_VER_1_5 = '1.5'
    SMIS_SPEC_VER_1_6 = '1.6'
    REG_ORG_CODE = pywbem.Uint16(11)


class Smis(IStorageAreaNetwork):
    """
    SMI-S plug-ing which exposes a small subset of the overall provided
    functionality of SMI-S
    """

    # SMI-S job 'JobState' enumerations
    (JS_NEW, JS_STARTING, JS_RUNNING, JS_SUSPENDED, JS_SHUTTING_DOWN,
     JS_COMPLETED,
     JS_TERMINATED, JS_KILLED, JS_EXCEPTION) = (2, 3, 4, 5, 6, 7, 8, 9, 10)

    # SMI-S job 'OperationalStatus' enumerations
    (JOB_OK, JOB_ERROR, JOB_STOPPED, JOB_COMPLETE) = (2, 6, 10, 17)

    # SMI-S invoke return values we are interested in
    # Reference: Page 54 in 1.5 SMI-S block specification
    (INVOKE_OK,
     INVOKE_NOT_SUPPORTED,
     INVOKE_TIMEOUT,
     INVOKE_FAILED,
     INVOKE_INVALID_PARAMETER,
     INVOKE_IN_USE,
     INVOKE_ASYNC,
     INVOKE_SIZE_NOT_SUPPORTED) = (0, 1, 3, 4, 5, 6, 4096, 4097)

    # SMI-S replication enumerations
    (SYNC_TYPE_MIRROR, SYNC_TYPE_SNAPSHOT, SYNC_TYPE_CLONE) = (6, 7, 8)

    # DMTF 2.29.1 (which SNIA SMI-S 1.6 based on)
    # CIM_StorageVolume['NameFormat']
    VOL_NAME_FORMAT_OTHER = 1
    VOL_NAME_FORMAT_VPD83_NNA6 = 2
    VOL_NAME_FORMAT_VPD83_NNA5 = 3
    VOL_NAME_FORMAT_VPD83_TYPE2 = 4
    VOL_NAME_FORMAT_VPD83_TYPE1 = 5
    VOL_NAME_FORMAT_VPD83_TYPE0 = 6
    VOL_NAME_FORMAT_SNVM = 7
    VOL_NAME_FORMAT_NODE_WWN = 8
    VOL_NAME_FORMAT_NNA = 9
    VOL_NAME_FORMAT_EUI64 = 10
    VOL_NAME_FORMAT_T10VID = 11

    # CIM_StorageVolume['NameNamespace']
    VOL_NAME_SPACE_OTHER = 1
    VOL_NAME_SPACE_VPD83_TYPE3 = 2
    VOL_NAME_SPACE_VPD83_TYPE2 = 3
    VOL_NAME_SPACE_VPD83_TYPE1 = 4
    VOL_NAME_SPACE_VPD80 = 5
    VOL_NAME_SPACE_NODE_WWN = 6
    VOL_NAME_SPACE_SNVM = 7

    JOB_RETRIEVE_NONE = 0
    JOB_RETRIEVE_VOLUME = 1
    JOB_RETRIEVE_POOL = 2

    # DMTF CIM 2.37.0 experimental CIM_StoragePool['Usage']
    DMTF_POOL_USAGE_SPARE = 8
    DMTF_POOL_USAGE_DELTA = 4

    # DMTF CIM 2.29.1 CIM_StorageConfigurationCapabilities
    # ['SupportedStorageElementFeatures']
    DMTF_SUPPORT_VOL_CREATE = 3

    # DMTF CIM 2.37.0 experimental CIM_StorageConfigurationCapabilities
    # ['SupportedStorageElementTypes']
    DMTF_ELEMENT_THICK_VOLUME = 2
    DMTF_ELEMENT_THIN_VOLUME = 5

    # DMTF CIM 2.29.1 CIM_StorageConfigurationCapabilities
    # ['SupportedStoragePoolFeatures']
    DMTF_ST_POOL_FEATURE_INEXTS = 2
    DMTF_ST_POOL_FEATURE_SINGLE_INPOOL = 3
    DMTF_ST_POOL_FEATURE_MULTI_INPOOL = 4

    # DMTF CIM 2.38.0+ CIM_StorageSetting['ThinProvisionedPoolType']
    DMTF_THINP_POOL_TYPE_ALLOCATED = pywbem.Uint16(7)

    # DMTF Disk Type
    DMTF_DISK_TYPE_UNKNOWN = 0
    DMTF_DISK_TYPE_OTHER = 1
    DMTF_DISK_TYPE_HDD = 2
    DMTF_DISK_TYPE_SSD = 3
    DMTF_DISK_TYPE_HYBRID = 4

    _DMTF_DISK_TYPE_2_LSM = {
        DMTF_DISK_TYPE_UNKNOWN: Disk.DISK_TYPE_UNKNOWN,
        DMTF_DISK_TYPE_OTHER: Disk.DISK_TYPE_OTHER,
        DMTF_DISK_TYPE_HDD: Disk.DISK_TYPE_HDD,
        DMTF_DISK_TYPE_SSD: Disk.DISK_TYPE_SSD,
        DMTF_DISK_TYPE_HYBRID: Disk.DISK_TYPE_HYBRID,
    }

    @staticmethod
    def dmtf_disk_type_2_lsm_disk_type(dmtf_disk_type):
        if dmtf_disk_type in Smis._DMTF_DISK_TYPE_2_LSM.keys():
            return Smis._DMTF_DISK_TYPE_2_LSM[dmtf_disk_type]
        else:
            return Disk.DISK_TYPE_UNKNOWN

    DMTF_STATUS_UNKNOWN = 0
    DMTF_STATUS_OTHER = 1
    DMTF_STATUS_OK = 2
    DMTF_STATUS_DEGRADED = 3
    DMTF_STATUS_STRESSED = 4
    DMTF_STATUS_PREDICTIVE_FAILURE = 5
    DMTF_STATUS_ERROR = 6
    DMTF_STATUS_NON_RECOVERABLE_ERROR = 7
    DMTF_STATUS_STARTING = 8
    DMTF_STATUS_STOPPING = 9
    DMTF_STATUS_STOPPED = 10
    DMTF_STATUS_IN_SERVICE = 11
    DMTF_STATUS_NO_CONTACT = 12
    DMTF_STATUS_LOST_COMMUNICATION = 13
    DMTF_STATUS_ABORTED = 14
    DMTF_STATUS_DORMANT = 15
    DMTF_STATUS_SUPPORTING_ENTITY_IN_ERROR = 16
    DMTF_STATUS_COMPLETED = 17
    DMTF_STATUS_POWER_MODE = 18

    # We will rework this once SNIA documented these out.
    _DMTF_STAUTS_TO_POOL_STATUS = {
        DMTF_STATUS_UNKNOWN: Pool.STATUS_UNKNOWN,
        DMTF_STATUS_OTHER: Pool.STATUS_OTHER,
        DMTF_STATUS_OK: Pool.STATUS_OK,
        DMTF_STATUS_DEGRADED: Pool.STATUS_DEGRADED,
        DMTF_STATUS_STRESSED: Pool.STATUS_STRESSED,
        DMTF_STATUS_PREDICTIVE_FAILURE: Pool.STATUS_OTHER,
        DMTF_STATUS_ERROR: Pool.STATUS_ERROR,
        DMTF_STATUS_NON_RECOVERABLE_ERROR: Pool.STATUS_ERROR,
        DMTF_STATUS_STARTING: Pool.STATUS_STARTING,
        DMTF_STATUS_STOPPING: Pool.STATUS_STOPPING,
        DMTF_STATUS_STOPPED: Pool.STATUS_STOPPED,
        DMTF_STATUS_IN_SERVICE: Pool.STATUS_OTHER,
        DMTF_STATUS_NO_CONTACT: Pool.STATUS_OTHER,
        DMTF_STATUS_LOST_COMMUNICATION: Pool.STATUS_OTHER,
        DMTF_STATUS_DORMANT: Pool.STATUS_DORMANT,
        DMTF_STATUS_SUPPORTING_ENTITY_IN_ERROR: Pool.STATUS_OTHER,
        DMTF_STATUS_COMPLETED: Pool.STATUS_OTHER,
        DMTF_STATUS_POWER_MODE: Pool.STATUS_OTHER,
    }

    _DMTF_STAUTS_TO_POOL_STATUS_INFO = {
        # TODO: Use CIM_RelatedElementCausingError
        #       to find out the error info.
        DMTF_STATUS_PREDICTIVE_FAILURE: 'Predictive failure',
        DMTF_STATUS_IN_SERVICE: 'In service',
        DMTF_STATUS_NO_CONTACT: 'No contact',
        DMTF_STATUS_LOST_COMMUNICATION: 'Lost communication',
        DMTF_STATUS_SUPPORTING_ENTITY_IN_ERROR: 'Supporting entity in error',
        DMTF_STATUS_COMPLETED: 'Completed',
        DMTF_STATUS_POWER_MODE: 'Power mode',
    }

    _DMTF_STAUTS_TO_DISK_STATUS = {
        DMTF_STATUS_UNKNOWN: Disk.STATUS_UNKNOWN,
        DMTF_STATUS_OTHER: Disk.STATUS_OTHER,
        DMTF_STATUS_OK: Disk.STATUS_OK,
        DMTF_STATUS_DEGRADED: Disk.STATUS_OTHER,
        DMTF_STATUS_STRESSED: Disk.STATUS_OTHER,
        DMTF_STATUS_PREDICTIVE_FAILURE: Disk.STATUS_PREDICTIVE_FAILURE,
        DMTF_STATUS_ERROR: Disk.STATUS_ERROR,
        DMTF_STATUS_NON_RECOVERABLE_ERROR: Disk.STATUS_ERROR,
        DMTF_STATUS_STARTING: Disk.STATUS_STARTING,
        DMTF_STATUS_STOPPING: Disk.STATUS_STOPPING,
        DMTF_STATUS_STOPPED: Disk.STATUS_STOPPED,
        DMTF_STATUS_IN_SERVICE: Disk.STATUS_OTHER,
        DMTF_STATUS_NO_CONTACT: Disk.STATUS_OTHER,
        DMTF_STATUS_LOST_COMMUNICATION: Disk.STATUS_OTHER,
        DMTF_STATUS_DORMANT: Disk.STATUS_OFFLINE,
        DMTF_STATUS_SUPPORTING_ENTITY_IN_ERROR: Disk.STATUS_OTHER,
        DMTF_STATUS_COMPLETED: Disk.STATUS_OTHER,
        DMTF_STATUS_POWER_MODE: Disk.STATUS_OTHER,
    }

    _DMTF_STAUTS_TO_DISK_STATUS_INFO = {
        DMTF_STATUS_DORMANT: 'Dormant',
        DMTF_STATUS_IN_SERVICE: 'In service',
        DMTF_STATUS_NO_CONTACT: 'No contact',
        DMTF_STATUS_LOST_COMMUNICATION: 'Lost communication',
        DMTF_STATUS_SUPPORTING_ENTITY_IN_ERROR: 'Supporting entity in error',
        DMTF_STATUS_COMPLETED: 'Completed',
        DMTF_STATUS_POWER_MODE: 'Power mode',
    }

    # DSP 1033  Profile Registration
    DMTF_INTEROP_NAMESPACES = ['interop', 'root/interop']
    SMIS_DEFAULT_NAMESPACE = 'interop'

    IAAN_WBEM_HTTP_PORT = 5988
    IAAN_WBEM_HTTPS_PORT = 5989

    class RepSvc(object):

        class Action(object):
            CREATE_ELEMENT_REPLICA = 2

        class RepTypes(object):
            # SMI-S replication service capabilities
            SYNC_MIRROR_LOCAL = 2
            ASYNC_MIRROR_LOCAL = 3
            SYNC_MIRROR_REMOTE = 4
            ASYNC_MIRROR_REMOTE = 5
            SYNC_SNAPSHOT_LOCAL = 6
            ASYNC_SNAPSHOT_LOCAL = 7
            SYNC_SNAPSHOT_REMOTE = 8
            ASYNC_SNAPSHOT_REMOTE = 9
            SYNC_CLONE_LOCAL = 10
            ASYNC_CLONE_LOCAL = 11
            SYNC_CLONE_REMOTE = 12
            ASYNC_CLONE_REMOTE = 13

    class CopyStates(object):
        INITIALIZED = 2
        UNSYNCHRONIZED = 3
        SYNCHRONIZED = 4
        INACTIVE = 8

    class CopyTypes(object):
        ASYNC = 2           # Async. mirror
        SYNC = 3            # Sync. mirror
        UNSYNCASSOC = 4     # lsm Clone
        UNSYNCUNASSOC = 5   # lsm Copy

    class Synchronized(object):
        class SyncState(object):
            INITIALIZED = 2
            PREPAREINPROGRESS = 3
            PREPARED = 4
            RESYNCINPROGRESS = 5
            SYNCHRONIZED = 6
            FRACTURE_IN_PROGRESS = 7
            QUIESCEINPROGRESS = 8
            QUIESCED = 9
            RESTORE_IN_PROGRESSS = 10
            IDLE = 11
            BROKEN = 12
            FRACTURED = 13
            FROZEN = 14
            COPY_IN_PROGRESS = 15

    # SMI-S mode for mirror updates
    (CREATE_ELEMENT_REPLICA_MODE_SYNC,
     CREATE_ELEMENT_REPLICA_MODE_ASYNC) = (2, 3)

    # SMI-S volume 'OperationalStatus' enumerations
    (VOL_OP_STATUS_OK, VOL_OP_STATUS_DEGRADED, VOL_OP_STATUS_ERR,
     VOL_OP_STATUS_STARTING,
     VOL_OP_STATUS_DORMANT) = (2, 3, 6, 8, 15)

    # SMI-S CIM_ComputerSystem OperationalStatus for system
    class SystemOperationalStatus(object):
        UNKNOWN = 0
        OTHER = 1
        OK = 2
        DEGRADED = 3
        STRESSED = 4
        PREDICTIVE_FAILURE = 5
        ERROR = 6
        NON_RECOVERABLE_ERROR = 7
        STARTING = 8
        STOPPING = 9
        STOPPED = 10
        IN_SERVICE = 11
        NO_CONTACT = 12
        LOST_COMMUNICATION = 13
        ABORTED = 14
        DORMANT = 15
        SUPPORTING_ENTITY_IN_ERROR = 16
        COMPLETED = 17
        POWER_MODE = 18

    # SMI-S ExposePaths device access enumerations
    (EXPOSE_PATHS_DA_READ_WRITE, EXPOSE_PATHS_DA_READ_ONLY) = (2, 3)

    def __init__(self):
        self._c = None
        self.tmo = 0
        self.system_list = None
        self.cim_rps = []
        self.cim_root_profile_dict = dict()
        self.fallback_mode = True    # Means we cannot use profile register
        self.all_vendor_namespaces = []

    def _get_cim_instance_by_id(self, class_type, requested_id,
                                property_list=None, raise_error=True):
        """
        Find out the CIM_XXXX Instance which holding the requested_id
        Return None when error and raise_error is False
        """
        class_name = Smis._cim_class_name_of(class_type)
        error_numer = Smis._not_found_error_of_class(class_type)
        id_pros = Smis._property_list_of_id(class_type, property_list)

        if property_list is None:
            property_list = id_pros
        else:
            property_list = _merge_list(property_list, id_pros)

        cim_xxxs = self._enumerate(class_name, property_list)
        org_requested_id = requested_id
        if class_type == 'Job':
            (requested_id, ignore) = self._parse_job_id(requested_id)
        for cim_xxx in cim_xxxs:
            if self._id(class_type, cim_xxx) == requested_id:
                return cim_xxx
        if raise_error is False:
            return None

        raise LsmError(error_numer,
                       "Cannot find %s Instance with " % class_name +
                       "%s ID '%s'" % (class_type, org_requested_id))

    def _get_class_instance(self, class_name, prop_name, prop_value,
                            raise_error=True, property_list=None):
        """
        Gets an instance of a class that optionally matches a specific
        property name and value
        """
        instances = None
        if property_list is None:
            property_list = [prop_name]
        else:
            property_list = _merge_list(property_list, [prop_name])

        try:
            cim_xxxs = self._enumerate(class_name, property_list)
        except CIMError as ce:
            error_code = tuple(ce)[0]

            if error_code == pywbem.CIM_ERR_INVALID_CLASS and \
               raise_error is False:
                return None
            else:
                raise

        for cim_xxx in cim_xxxs:
            if prop_name in cim_xxx and cim_xxx[prop_name] == prop_value:
                return cim_xxx

        if raise_error:
            raise LsmError(ErrorNumber.LSM_BUG,
                           "Unable to find class instance %s " % class_name +
                           "with property %s " % prop_name +
                           "with value %s" % prop_value)
        return None

    def _pi(self, msg, retrieve_data, rc, out):
        """
        Handle the the process of invoking an operation.
        """
        # Check to see if operation is done
        if rc == Smis.INVOKE_OK:
            if retrieve_data == Smis.JOB_RETRIEVE_VOLUME:
                return None, self._new_vol_from_name(out)
            elif retrieve_data == Smis.JOB_RETRIEVE_POOL:
                return None, self._new_pool_from_name(out)
            else:
                return None, None

        elif rc == Smis.INVOKE_ASYNC:
            # We have an async operation
            job_id = self._job_id(out['Job'], retrieve_data)
            return job_id, None
        elif rc == Smis.INVOKE_NOT_SUPPORTED:
            raise LsmError(
                ErrorNumber.NO_SUPPORT,
                'SMI-S error code indicates operation not supported')
        else:
            raise LsmError(ErrorNumber.PLUGIN_ERROR,
                           'Error: ' + msg + " rc= " + str(rc))

    @handle_cim_errors
    def plugin_register(self, uri, password, timeout, flags=0):
        """
        Called when the plug-in runner gets the start request from the client.
        Checkout interop support status via:
            1. Enumerate CIM_RegisteredProfile in 'interop' namespace.
            2. if nothing found, then
               Enumerate CIM_RegisteredProfile in 'root/interop' namespace.
            3. if nothing found, then
               Enumerate CIM_RegisteredProfile in userdefined namespace.
        """
        protocol = 'http'
        port = Smis.IAAN_WBEM_HTTP_PORT
        u = uri_parse(uri, ['scheme', 'netloc', 'host'], None)

        if u['scheme'].lower() == 'smispy+ssl':
            protocol = 'https'
            port = Smis.IAAN_WBEM_HTTPS_PORT

        if 'port' in u:
            port = u['port']

        url = "%s://%s:%s" % (protocol, u['host'], port)

        # System filtering
        self.system_list = None

        namespace = None
        if 'namespace' in u['parameters']:
            namespace = u['parameters']['namespace']
            self.all_vendor_namespaces = [namespace]
        else:
            namespace = Smis.SMIS_DEFAULT_NAMESPACE

        if 'systems' in u['parameters']:
            self.system_list = split(u['parameters']["systems"], ":")

        if namespace is not None:
            self._c = pywbem.WBEMConnection(url, (u['username'], password),
                                            namespace)
            if "no_ssl_verify" in u["parameters"] \
               and u["parameters"]["no_ssl_verify"] == 'yes':
                try:
                    self._c = pywbem.WBEMConnection(
                        url,
                        (u['username'], password),
                        namespace,
                        no_verification=True)
                except TypeError:
                    # pywbem is not holding fix from
                    # https://bugzilla.redhat.com/show_bug.cgi?id=1039801
                    pass

        self.tmo = timeout

        if 'force_fallback_mode' in u['parameters'] and \
           u['parameters']['force_fallback_mode'] == 'yes':
            return

        # Checking profile registration support status unless
        # force_fallback_mode is enabled in URI.
        namespace_check_list = Smis.DMTF_INTEROP_NAMESPACES
        if 'namespace' in u['parameters'] and \
           u['parameters']['namespace'] not in namespace_check_list:
            namespace_check_list.extend([u['parameters']['namespace']])

        for interop_namespace in Smis.DMTF_INTEROP_NAMESPACES:
            try:
                self.cim_rps = self._c.EnumerateInstances(
                    'CIM_RegisteredProfile',
                    namespace=interop_namespace,
                    PropertyList=['RegisteredName', 'RegisteredVersion',
                                  'RegisteredOrganization'],
                    LocalOnly=False)
            except CIMError as e:
                if e[0] == pywbem.CIM_ERR_NOT_SUPPORTED or \
                   e[0] == pywbem.CIM_ERR_INVALID_NAMESPACE or \
                   e[0] == pywbem.CIM_ERR_INVALID_CLASS:
                    pass
                else:
                    raise
            if len(self.cim_rps) != 0:
                break

        if len(self.cim_rps) >= 1:
            self.fallback_mode = False
            self.all_vendor_namespaces = []
            # Support 'Array' profile is step 0 for this whole plugin.
            # We find out all 'Array' CIM_RegisteredProfile and stored
            # them into self.cim_root_profile_dict
            if not self._profile_is_supported(
                    SNIA.BLK_ROOT_PROFILE,
                    SNIA.SMIS_SPEC_VER_1_4,
                    strict=False):
                raise LsmError(ErrorNumber.NO_SUPPORT,
                               "Target SMI-S provider does not support "
                               "SNIA SMI-S SPEC %s '%s' profile" %
                               (SNIA.SMIS_SPEC_VER_1_4,
                                SNIA.BLK_ROOT_PROFILE))

    def time_out_set(self, ms, flags=0):
        self.tmo = ms

    def time_out_get(self, flags=0):
        return self.tmo

    def plugin_unregister(self, flags=0):
        self._c = None

    def _scs_supported_capabilities(self, system, cap):
        """
        Interrogate the supported features of the Storage Configuration
        service
        """
        scs = self._get_class_instance('CIM_StorageConfigurationService',
                                       'SystemName', system.id)

        if scs is not None:
            scs_cap_inst = self._c.Associators(
                scs.path,
                AssocClass='CIM_ElementCapabilities',
                ResultClass='CIM_StorageConfigurationCapabilities')[0]

            if scs_cap_inst is not None:
                # print 'Async', scs_cap_inst['SupportedAsynchronousActions']
                # print 'Sync', scs_cap_inst['SupportedSynchronousActions']
                async = None
                sync = None

                if 'SupportedAsynchronousActions' in scs_cap_inst:
                    async = scs_cap_inst['SupportedAsynchronousActions']
                if 'SupportedSynchronousActions' in scs_cap_inst:
                    sync = scs_cap_inst['SupportedSynchronousActions']

                if async is None:
                    async = []

                if sync is None:
                    sync = []

                combined = async
                combined.extend(sync)

                #TODO Get rid of magic numbers
                if 'SupportedStorageElementTypes' in scs_cap_inst:
                    if 2 in scs_cap_inst['SupportedStorageElementTypes']:
                        cap.set(Capabilities.VOLUMES)

                if 5 in combined:
                    cap.set(Capabilities.VOLUME_CREATE)

                if 6 in combined:
                    cap.set(Capabilities.VOLUME_DELETE)

                if 7 in combined:
                    cap.set(Capabilities.VOLUME_RESIZE)

    def _rs_supported_capabilities(self, system, cap):
        """
        Interrogate the supported features of the replication service
        """
        rs = self._get_class_instance("CIM_ReplicationService", 'SystemName',
                                      system.id, raise_error=False)
        if rs:
            rs_cap = self._c.Associators(
                rs.path,
                AssocClass='CIM_ElementCapabilities',
                ResultClass='CIM_ReplicationServiceCapabilities')[0]

            s_rt = rs_cap['SupportedReplicationTypes']

            if self.RepSvc.Action.CREATE_ELEMENT_REPLICA in s_rt or \
                    self.RepSvc.Action.CREATE_ELEMENT_REPLICA in s_rt:
                cap.set(Capabilities.VOLUME_REPLICATE)

            # Mirror support is not working and is not supported at this time.
            # if self.RepSvc.RepTypes.SYNC_MIRROR_LOCAL in s_rt:
            #    cap.set(Capabilities.DeviceID)

            # if self.RepSvc.RepTypes.ASYNC_MIRROR_LOCAL \
            #    in s_rt:
            #    cap.set(Capabilities.VOLUME_REPLICATE_MIRROR_ASYNC)

            if self.RepSvc.RepTypes.SYNC_SNAPSHOT_LOCAL in s_rt or \
                    self.RepSvc.RepTypes.ASYNC_SNAPSHOT_LOCAL in s_rt:
                cap.set(Capabilities.VOLUME_REPLICATE_CLONE)

            if self.RepSvc.RepTypes.SYNC_CLONE_LOCAL in s_rt or \
               self.RepSvc.RepTypes.ASYNC_CLONE_LOCAL in s_rt:
                cap.set(Capabilities.VOLUME_REPLICATE_COPY)
        else:
            # Try older storage configuration service

            rs = self._get_class_instance("CIM_StorageConfigurationService",
                                          'SystemName',
                                          system.id, raise_error=False)

            if rs:
                rs_cap = self._c.Associators(
                    rs.path,
                    AssocClass='CIM_ElementCapabilities',
                    ResultClass='CIM_StorageConfigurationCapabilities')[0]

                if rs_cap is not None and 'SupportedCopyTypes' in rs_cap:
                    sct = rs_cap['SupportedCopyTypes']

                    if len(sct):
                        cap.set(Capabilities.VOLUME_REPLICATE)

                    # Mirror support is not working and is not supported at
                    # this time.

                    # if Smis.CopyTypes.ASYNC in sct:
                    #    cap.set(Capabilities.VOLUME_REPLICATE_MIRROR_ASYNC)

                    # if Smis.CopyTypes.SYNC in sct:
                    #    cap.set(Capabilities.VOLUME_REPLICATE_MIRROR_SYNC)

                        if Smis.CopyTypes.UNSYNCASSOC in sct:
                            cap.set(Capabilities.VOLUME_REPLICATE_CLONE)

                        if Smis.CopyTypes.UNSYNCUNASSOC in sct:
                            cap.set(Capabilities.VOLUME_REPLICATE_COPY)

    def _pcm_supported_capabilities(self, system, cap):
        """
        Interrogate the supported features of
        CIM_ProtocolControllerMaskingCapabilities
        """

        # Get the cim object that represents the system
        cim_sys = None
        cim_pcms = None
        cim_sys = self._get_cim_instance_by_id('System', system.id)
        if self.fallback_mode:

            # Using 'ExposePathsSupported of
            # CIM_ProtocolControllerMaskingCapabilities
            # to check support status of HidePaths() and ExposePaths() is
            # not documented by SNIA SMI-S 1.4 or 1.6, but only defined in
            # DMTF CIM MOF files.
            try:
                cim_pcms = self._c.Associators(
                    cim_sys.path,
                    ResultClass='CIM_ProtocolControllerMaskingCapabilities')
            except CIMError as e:
                if e[0] == pywbem.CIM_ERR_NOT_SUPPORTED or \
                   e[0] == pywbem.CIM_ERR_INVALID_CLASS:
                    return
            if cim_pcms is not None and len(cim_pcms) == 1:
                cap.set(Capabilities.ACCESS_GROUPS)
                cap.set(Capabilities.ACCESS_GROUPS_GRANTED_TO_VOLUME)
                cap.set(Capabilities.VOLUMES_ACCESSIBLE_BY_ACCESS_GROUP)

                if cim_pcms[0]['ExposePathsSupported']:
                    cap.set(Capabilities.VOLUME_MASK)
                    cap.set(Capabilities.VOLUME_UNMASK)
                    cap.set(Capabilities.ACCESS_GROUP_INITIATOR_ADD)
                    cap.set(Capabilities.ACCESS_GROUP_INITIATOR_DELETE)
                return
        else:
            # Since SNIA SMI-S 1.4rev6:
            # CIM_ControllerConfigurationService is mandatory
            # and it's ExposePaths() and HidePaths() are mandatory
            cap.set(Capabilities.ACCESS_GROUPS)
            cap.set(Capabilities.VOLUME_MASK)
            cap.set(Capabilities.VOLUME_UNMASK)
            cap.set(Capabilities.ACCESS_GROUP_INITIATOR_ADD)
            cap.set(Capabilities.ACCESS_GROUP_INITIATOR_DELETE)
            cap.set(Capabilities.ACCESS_GROUPS_GRANTED_TO_VOLUME)
            cap.set(Capabilities.VOLUMES_ACCESSIBLE_BY_ACCESS_GROUP)

    def _common_capabilities(self, system):
        cap = Capabilities()

        # Assume that the SMI-S we are talking to supports blocks
        cap.set(Capabilities.BLOCK_SUPPORT)

        self._scs_supported_capabilities(system, cap)
        self._rs_supported_capabilities(system, cap)
        return cap

    def _tgt_port_capabilities(self, system, cap):
        flag_fc_support = False
        flag_iscsi_support = False
        if self.fallback_mode:
            flag_fc_support = True
            flag_iscsi_support = True
            # CIM_FCPort is the contral class of FC Targets profile
            try:
                self._enumerate('CIM_FCPort')
            except CIMError as e:
                if e[0] == pywbem.CIM_ERR_NOT_SUPPORTED or \
                   e[0] == pywbem.CIM_ERR_INVALID_CLASS:
                    flag_fc_support = False

            # Even CIM_EthernetPort is the contral class of iSCSI Target
            # Ports profile, but that class is optional. :(
            # We use CIM_iSCSIProtocolEndpoint as it's a start point we are
            # using in our code of target_ports().
            try:
                self._enumerate('CIM_iSCSIProtocolEndpoint')
            except CIMError as e:
                if e[0] == pywbem.CIM_ERR_NOT_SUPPORTED or \
                   e[0] == pywbem.CIM_ERR_INVALID_CLASS:
                    flag_iscsi_support = False
        else:
            flag_fc_support = self._profile_is_supported(
                SNIA.FC_TGT_PORT_PROFILE,
                SNIA.SMIS_SPEC_VER_1_4,
                strict=False,
                raise_error=False)
            # One more check for NetApp Typo:
            #   NetApp:     'FC Target Port'
            #   SMI-S:      'FC Target Ports'
            # Bug reported.
            if not flag_fc_support:
                flag_fc_support = self._profile_is_supported(
                    'FC Target Port',
                    SNIA.SMIS_SPEC_VER_1_4,
                    strict=False,
                    raise_error=False)
            flag_iscsi_support = self._profile_is_supported(
                SNIA.ISCSI_TGT_PORT_PROFILE,
                SNIA.SMIS_SPEC_VER_1_4,
                strict=False,
                raise_error=False)

        if flag_fc_support or flag_iscsi_support:
            cap.set(Capabilities.TARGET_PORTS)
        return

    @handle_cim_errors
    def capabilities(self, system, flags=0):
        cap = self._common_capabilities(system)
        self._pcm_supported_capabilities(system, cap)
        self._tgt_port_capabilities(system, cap)
        return cap

    @handle_cim_errors
    def plugin_info(self, flags=0):
        return "Generic SMI-S support", VERSION

    @staticmethod
    def _job_completed_ok(status):
        """
        Given a concrete job instance, check the operational status.  This
        is a little convoluted as different SMI-S proxies return the values in
        different positions in list :-)
        """
        rc = False
        op = status['OperationalStatus']

        if (len(op) > 1 and
            ((op[0] == Smis.JOB_OK and op[1] == Smis.JOB_COMPLETE) or
             (op[0] == Smis.JOB_COMPLETE and op[1] == Smis.JOB_OK))):
            rc = True

        return rc

    @handle_cim_errors
    def job_status(self, job_id, flags=0):
        """
        Given a job id returns the current status as a tuple
        (status (enum), percent_complete(integer), volume (None or Volume))
        """
        completed_item = None

        props = ['JobState', 'PercentComplete', 'ErrorDescription',
                 'OperationalStatus']
        cim_job_pros = self._property_list_of_id('Job', props)

        cim_job = self._get_cim_instance_by_id('Job', job_id, cim_job_pros)

        job_state = cim_job['JobState']

        if job_state in (Smis.JS_NEW, Smis.JS_STARTING, Smis.JS_RUNNING):
            status = JobStatus.INPROGRESS

            pc = cim_job['PercentComplete']
            if pc > 100:
                percent_complete = 100
            else:
                percent_complete = pc

        elif job_state == Smis.JS_COMPLETED:
            status = JobStatus.COMPLETE
            percent_complete = 100

            if Smis._job_completed_ok(cim_job):
                (ignore, retrieve_data) = self._parse_job_id(job_id)
                if retrieve_data == Smis.JOB_RETRIEVE_VOLUME:
                    completed_item = self._new_vol_from_job(cim_job)
                elif retrieve_data == Smis.JOB_RETRIEVE_POOL:
                    completed_item = self._new_pool_from_job(cim_job)
            else:
                status = JobStatus.ERROR

        else:
            raise LsmError(ErrorNumber.PLUGIN_ERROR,
                           str(cim_job['ErrorDescription']))

        return status, percent_complete, completed_item

    @staticmethod
    def _cim_class_name_of(class_type):
        if class_type == 'Volume':
            return 'CIM_StorageVolume'
        if class_type == 'System':
            return 'CIM_ComputerSystem'
        if class_type == 'Pool':
            return 'CIM_StoragePool'
        if class_type == 'Disk':
            return 'CIM_DiskDrive'
        if class_type == 'Job':
            return 'CIM_ConcreteJob'
        if class_type == 'AccessGroup':
            return 'CIM_SCSIProtocolController'
        if class_type == 'Initiator':
            return 'CIM_StorageHardwareID'
        raise LsmError(ErrorNumber.LSM_BUG,
                       "Smis._cim_class_name_of() got unknown " +
                       "class_type %s" % class_type)

    @staticmethod
    def _not_found_error_of_class(class_type):
        if class_type == 'Volume':
            return ErrorNumber.NOT_FOUND_VOLUME
        if class_type == 'System':
            return ErrorNumber.NOT_FOUND_SYSTEM
        if class_type == 'Pool':
            return ErrorNumber.NOT_FOUND_POOL
        if class_type == 'Disk':
            return ErrorNumber.NOT_FOUND_DISK
        if class_type == 'Job':
            return ErrorNumber.NOT_FOUND_JOB
        if class_type == 'AccessGroup':
            return ErrorNumber.NOT_FOUND_ACCESS_GROUP
        if class_type == 'Initiator':
            return ErrorNumber.INVALID_ARGUMENT
        raise LsmError(ErrorNumber.LSM_BUG,
                       "Smis._cim_class_name_of() got unknown " +
                       "class_type %s" % class_type)

    @staticmethod
    def _property_list_of_id(class_type, extra_properties=None):
        """
        Return a PropertyList which the ID of current class is basing on
        """
        rc = []
        if class_type == 'Volume':
            rc = ['SystemName', 'DeviceID']
        elif class_type == 'System':
            rc = ['Name']
        elif class_type == 'Pool':
            rc = ['InstanceID']
        elif class_type == 'SystemChild':
            rc = ['SystemName']
        elif class_type == 'Disk':
            rc = ['SystemName', 'DeviceID']
        elif class_type == 'Job':
            rc = ['InstanceID']
        elif class_type == 'AccessGroup':
            rc = ['DeviceID']
        elif class_type == 'Initiator':
            rc = ['StorageID']
        else:
            raise LsmError(ErrorNumber.LSM_BUG,
                           "Smis._cim_class_name_of() got unknown " +
                           "class_type %s" % class_type)

        if extra_properties:
            rc = _merge_list(rc, extra_properties)
        return rc

    def _sys_id_child(self, cim_xxx):
        """
        Find out the system id of Pool/Volume/Disk/AccessGroup/Initiator
        Currently, we just use SystemName of cim_xxx
        """
        return self._id('SystemChild', cim_xxx)

    def _sys_id(self, cim_sys):
        """
        Return CIM_ComputerSystem['Name']
        """
        return self._id('System', cim_sys)

    def _pool_id(self, cim_pool):
        """
        Return CIM_StoragePool['InstanceID']
        """
        return self._id('Pool', cim_pool)

    def _vol_id(self, cim_vol):
        """
        Return the MD5 hash of CIM_StorageVolume['SystemName'] and
        ['DeviceID']
        """
        return self._id('Volume', cim_vol)

    def _disk_id(self, cim_disk):
        """
        Return the MD5 hash of CIM_DiskDrive['SystemName'] and ['DeviceID']
        """
        return self._id('Disk', cim_disk)

    def _job_id(self, cim_job, retrieve_data):
        """
        Return the MD5 has of CIM_ConcreteJob['InstanceID'] in conjunction
        with '@%s' % retrieve_data
        retrieve_data should be JOB_RETRIEVE_NONE or JOB_RETRIEVE_VOLUME or etc
        """
        return "%s@%d" % (self._id('Job', cim_job), int(retrieve_data))

    def _access_group_id(self, cim_ag):
        """
        Retrive Access Group ID from CIM_SCSIProtocolController['DeviceID']
        """
        return self._id('AccessGroup', cim_ag)

    def _init_id(self, cim_init):
        """
        Retrive Initiator ID from CIM_StorageHardwareID
        """
        return self._id('Initiator', cim_init)

    def _id(self, class_type, cim_xxx):
        """
        Return the ID of certain class.
        When ID is based on two or more properties, we use MD5 hash of them.
        If not, return the property value.
        """
        property_list = Smis._property_list_of_id(class_type)
        for key in property_list:
            if key not in cim_xxx:
                cim_xxx = self._c.GetInstance(cim_xxx.path,
                                              PropertyList=property_list,
                                              LocalOnly=False)
                break

        id_str = ''
        for key in property_list:
            if key not in cim_xxx:
                cim_class_name = ''
                if class_type == 'SystemChild':
                    cim_class_name = str(cim_xxx.classname)
                else:
                    cim_class_name = Smis._cim_class_name_of(class_type)
                raise LsmError(ErrorNumber.NO_SUPPORT,
                               "%s %s " % (cim_class_name, cim_xxx.path) +
                               "does not have property %s " % str(key) +
                               "calculate out %s id" % class_type)
            else:
                id_str += cim_xxx[key]
        if len(property_list) == 1 and class_type != 'Job':
            return id_str
        else:
            return md5(id_str)

    @staticmethod
    def _parse_job_id(job_id):
        """
        job_id is assembled by a md5 string and retrieve_data
        This method will split it and return (md5_str, retrieve_data)
        """
        tmp_list = job_id.split('@', 2)
        md5_str = tmp_list[0]
        retrieve_data = Smis.JOB_RETRIEVE_NONE
        if len(tmp_list) == 2:
            retrieve_data = int(tmp_list[1])
        return (md5_str, retrieve_data)

    def _get_pool_from_vol(self, cim_vol):
        """
         Takes a CIMInstance that represents a volume and returns the pool
         id for that volume.
        """
        property_list = Smis._property_list_of_id('Pool')
        cim_pool = self._c.Associators(
            cim_vol.path,
            AssocClass='CIM_AllocatedFromStoragePool',
            ResultClass='CIM_StoragePool',
            PropertyList=property_list)[0]
        return self._pool_id(cim_pool)

    @staticmethod
    def _get_vol_other_id_info(cv):
        other_id = None

        if 'OtherIdentifyingInfo' in cv \
                and cv["OtherIdentifyingInfo"] is not None \
                and len(cv["OtherIdentifyingInfo"]) > 0:

            other_id = cv["OtherIdentifyingInfo"]

            if isinstance(other_id, list):
                other_id = other_id[0]

            # This is not what we are looking for if the field has this value
            if other_id is not None and other_id == "VPD83Type3":
                other_id = None

        return other_id

    def _new_vol_cim_vol_pros(self):
        """
        Retrun the PropertyList required for creating new LSM Volume.
        """
        props = ['OperationalStatus', 'ElementName', 'NameFormat',
                 'NameNamespace', 'BlockSize', 'NumberOfBlocks', 'Name',
                 'OtherIdentifyingInfo', 'IdentifyingDescriptions', 'Usage']
        cim_vol_pros = self._property_list_of_id("Volume", props)
        return cim_vol_pros

    def _new_vol(self, cv, pool_id=None, sys_id=None):
        """
        Takes a CIMInstance that represents a volume and returns a lsm Volume
        """

        # Reference page 134 in 1.5 spec.
        status = Volume.STATUS_UNKNOWN

        # OperationalStatus is mandatory
        if 'OperationalStatus' in cv:
            for s in cv["OperationalStatus"]:
                if s == Smis.VOL_OP_STATUS_OK:
                    status |= Volume.STATUS_OK
                elif s == Smis.VOL_OP_STATUS_DEGRADED:
                    status |= Volume.STATUS_DEGRADED
                elif s == Smis.VOL_OP_STATUS_ERR:
                    status |= Volume.STATUS_ERR
                elif s == Smis.VOL_OP_STATUS_STARTING:
                    status |= Volume.STATUS_STARTING
                elif s == Smis.VOL_OP_STATUS_DORMANT:
                    status |= Volume.STATUS_DORMANT

        # This is optional (User friendly name)
        if 'ElementName' in cv:
            user_name = cv["ElementName"]
        else:
            #Better fallback value?
            user_name = cv['DeviceID']

        vpd_83 = Smis._vpd83_in_cv_name(cv)
        if vpd_83 is None:
            vpd_83 = Smis._vpd83_in_cv_otherinfo(cv)

        if vpd_83 is None:
            vpd_83 = Smis._vpd83_in_cv_ibm_xiv(cv)

        if vpd_83 is None:
            vpd_83 = ''

        #This is a fairly expensive operation, so it's in our best interest
        #to not call this very often.
        if pool_id is None:
            #Go an retrieve the pool id
            pool_id = self._get_pool_from_vol(cv)

        if sys_id is None:
            sys_id = cv['SystemName']

        return Volume(self._vol_id(cv), user_name, vpd_83, cv["BlockSize"],
                      cv["NumberOfBlocks"], status, sys_id, pool_id)

    @staticmethod
    def _vpd83_in_cv_name(cv):
        """
        Explanation of these Format is in:
          SMI-S 1.6 r4 SPEC part1 7.6.2 Table 2 Page 38, PDF Page 60:
              Table 2 - Standard Formats for StorageVolume Names
        Only these combinations is allowed when storing VPD83 in cv["Name"]:
         * NameFormat = NAA(9), NameNamespace = VPD83Type3(1)
            SCSI VPD page 83, type 3h, Association=0, NAA 0101b/0110b/0010b
            NAA name with first nibble of 2/5/6.
            Formatted as 16 or 32 un-separated upper case hex digits
         * NameFormat = NAA(9), NameNamespace = VPD83Type3(2)
            SCSI VPD page 83, type 3h, Association=0, NAA 0001b
            NAA name with first nibble of 1. Formatted as 16 un-separated
            upper case hex digits
         * NameFormat = EUI64(10), NameNamespace = VPD83Type2(3)
            SCSI VPD page 83, type 2h, Association=0
            Formatted as 16, 24, or 32 un-separated upper case hex digits
         * NameFormat = T10VID(11), NameNamespace = VPD83Type1(4)
            SCSI VPD page 83, type 1h, Association=0
            Formatted as 1 to 252 bytes of ASCII.
        Will return vpd_83 if found.
        """
        if not ('NameFormat' in cv and
                'NameNamespace' in cv and
                'Name' in cv):
            return None
        nf = cv['NameFormat']
        nn = cv['NameNamespace']
        name = cv['Name']
        if not (nf and nn and name):
            return None
        # SNIA might missly said VPD83Type3(1), it should be
        # VOL_NAME_FORMAT_OTHER(1) based on DMTF.
        # Will remove the Smis.VOL_NAME_FORMAT_OTHER condition if confirmed as
        # SNIA document fault.
        if (nf == Smis.VOL_NAME_FORMAT_NNA and
                nn == Smis.VOL_NAME_FORMAT_OTHER) or \
           (nf == Smis.VOL_NAME_FORMAT_NNA and
                nn == Smis.VOL_NAME_SPACE_VPD83_TYPE3) or \
           (nf == Smis.VOL_NAME_FORMAT_EUI64 and
                nn == Smis.VOL_NAME_SPACE_VPD83_TYPE2) or \
           (nf == Smis.VOL_NAME_FORMAT_T10VID and
                nn == Smis.VOL_NAME_SPACE_VPD83_TYPE1):
            return name

    @staticmethod
    def _vpd83_in_cv_otherinfo(cv):
        """
        In SNIA SMI-S 1.6 r4 part 1 section 7.6.2: "Standard Formats for
        Logical Unit Names" it allow VPD83 stored in 'OtherIdentifyingInfo'
        Quote:
            Storage volumes may have multiple standard names. A page 83
            logical unit identifier shall be placed in the Name property with
            NameFormat and Namespace set as specified in Table 2. Each
            additional name should be placed in an element of
            OtherIdentifyingInfo. The corresponding element in
            IdentifyingDescriptions shall contain a string from the Values
            lists from NameFormat and NameNamespace, separated by a
            semi-colon. For example, an identifier from SCSI VPD page 83 with
            type 3, association 0, and NAA 0101b - the corresponding entry in
            IdentifyingDescriptions[] shall be "NAA;VPD83Type3".
        Will return the vpd_83 value if found
        """
        vpd83_namespaces = ['NAA;VPD83Type1', 'NAA;VPD83Type3',
                            'EUI64;VPD83Type2', 'T10VID;VPD83Type1']
        if not ("IdentifyingDescriptions" in cv and
                "OtherIdentifyingInfo" in cv):
            return None
        id_des = cv["IdentifyingDescriptions"]
        other_info = cv["OtherIdentifyingInfo"]
        if not (isinstance(cv["IdentifyingDescriptions"], list) and
                isinstance(cv["OtherIdentifyingInfo"], list)):
            return None

        index = 0
        len_id_des = len(id_des)
        len_other_info = len(other_info)
        while index < min(len_id_des, len_other_info):
            if [1 for x in vpd83_namespaces if x == id_des[index]]:
                return other_info[index]
            index += 1
        return None

    @staticmethod
    def _vpd83_in_cv_ibm_xiv(cv):
        """
        IBM XIV IBM.2810-MX90014 is not following SNIA standard.
        They are using NameFormat=NodeWWN(8) and
        NameNamespace=NodeWWN(6) and no otherinfo indicated the
        VPD 83 info.
        Its cv["Name"] is equal to VPD 83, will use it.
        """
        if not "CreationClassName" in cv:
            return None
        if cv["CreationClassName"] == "IBMTSDS_SEVolume":
            if "Name" in cv and cv["Name"]:
                return cv["Name"]

    def _new_vol_from_name(self, out):
        """
        Given a volume by CIMInstanceName, return a lsm Volume object
        """
        instance = None

        if 'TheElement' in out:
            instance = self._c.GetInstance(out['TheElement'],
                                           LocalOnly=False)
        elif 'TargetElement' in out:
            instance = self._c.GetInstance(out['TargetElement'],
                                           LocalOnly=False)

        return self._new_vol(instance)

    def _new_pool_from_name(self, out):
        """
        For SYNC CreateOrModifyElementFromStoragePool action.
        The new CIM_StoragePool is stored in out['Pool']
        """
        pool_pros = self._new_pool_cim_pool_pros()

        if 'Pool' in out:
            cim_new_pool = self._c.GetInstance(
                out['Pool'],
                PropertyList=pool_pros, LocalOnly=False)
            return self._new_pool(cim_new_pool)
        else:
            raise LsmError(ErrorNumber.LSM_BUG,
                           "Got not new Pool from out of InvokeMethod" +
                           "when CreateOrModifyElementFromStoragePool")

    def _cim_ag_pros(self):
        """
        Return a list of properties required to build new AccessGroup.
        """
        cim_ag_pros = self._property_list_of_id('AccessGroup')
        cim_ag_pros.extend(self._property_list_of_id('SystemChild'))
        cim_ag_pros.extend(['ElementName', 'StorageID'])
        cim_ag_pros.extend(['EMCAdapterRole'])  # EMC specific, used to
                                                 # filter out the mapping SPC.
        return cim_ag_pros

    def _cim_ag_to_lsm(self, cim_ag, system_id=None):
        if system_id is None:
            system_id = self._sys_id_child(cim_ag)
        ag_id = self._access_group_id(cim_ag)
        ag_name = cim_ag['ElementName']
        ag_init_ids = []
        cim_init_pros = self._property_list_of_id('Initiator')
        cim_init_pros.extend(['IDType'])
        cim_inits = self._cim_init_of(cim_ag, cim_init_pros)
        ag_init_ids = [self._init_id(i) for i in cim_inits]
        ag_init_types = [_dmtf_init_type_to_lsm(i) for i in cim_inits]
        init_type = AccessGroup.INIT_TYPE_UNKNOWN
        ag_init_type_dict = {}
        for ag_init_type in ag_init_types:
            ag_init_type_dict[ag_init_type] = 1
        if len(ag_init_type_dict) == 1:
            init_type = ag_init_types[0]
        elif (len(ag_init_type_dict) == 2 and
              AccessGroup.INIT_TYPE_ISCSI_IQN in ag_init_type_dict.keys() and
              AccessGroup.INIT_TYPE_WWPN in ag_init_type_dict.keys()):
            init_type = AccessGroup.INIT_TYPE_ISCSI_WWPN_MIXED
        else:
            # We have unknown mixed initiator type
            init_type = AccessGroup.INIT_TYPE_OTHER

        sys_id = self._sys_id_child(cim_ag)
        return AccessGroup(ag_id, ag_name, ag_init_ids, init_type, sys_id)

    def _new_vol_from_job(self, job):
        """
        Given a concrete job instance, return referenced volume as lsm volume
        """
        for a in self._c.Associators(job.path,
                                     AssocClass='CIM_AffectedJobElement',
                                     ResultClass='CIM_StorageVolume'):
            return self._new_vol(self._c.GetInstance(a.path, LocalOnly=False))
        return None

    def _new_pool_from_job(self, cim_job):
        """
        Given a CIMInstance of CIM_ConcreteJob, return a LSM Pool
        """
        pool_pros = self._new_pool_cim_pool_pros()
        cim_pools = self._c.Associators(cim_job.path,
                                        AssocClass='CIM_AffectedJobElement',
                                        ResultClass='CIM_StoragePool',
                                        PropertyList=pool_pros)
        return self._new_pool(cim_pools[0])

    @handle_cim_errors
    def volumes(self, search_key=None, search_value=None, flags=0):
        """
        Return all volumes.
        We are basing on "Block Services Package" profile version 1.4 or
        later:
            CIM_ComputerSystem
                 |
                 |  (CIM_HostedStoragePool)
                 |
                 v
            CIM_StoragePool
                 |
                 | (CIM_AllocatedFromStoragePool)
                 |
                 v
            CIM_StorageVolume
        As 'Block Services Package' is mandatory for 'Array' profile, we
        don't check support status here as startup() already checked 'Array'
        profile.
        """
        rc = []
        cim_sys_pros = self._property_list_of_id("System")
        cim_syss = self._root_cim_syss(cim_sys_pros)
        cim_vol_pros = self._new_vol_cim_vol_pros()
        for cim_sys in cim_syss:
            sys_id = self._sys_id(cim_sys)
            pool_pros = self._property_list_of_id('Pool')
            for cim_pool in self._cim_pools_of(cim_sys.path, pool_pros):
                pool_id = self._pool_id(cim_pool)
                cim_vols = self._c.Associators(
                    cim_pool.path,
                    AssocClass='CIM_AllocatedFromStoragePool',
                    ResultClass='CIM_StorageVolume',
                    PropertyList=cim_vol_pros)
                for cim_vol in cim_vols:
                    # Exclude those volumes which are reserved for system
                    if 'Usage' in cim_vol:
                        if cim_vol['Usage'] != 3:
                            vol = self._new_vol(cim_vol, pool_id, sys_id)
                            rc.extend([vol])
                    else:
                        vol = self._new_vol(cim_vol, pool_id, sys_id)
                        rc.extend([vol])
        return search_property(rc, search_key, search_value)

    def _cim_pools_of(self, cim_sys_path, property_list=None):
        if property_list is None:
            property_list = ['Primordial']
        else:
            property_list = _merge_list(property_list, ['Primordial'])

        cim_pools = self._c.Associators(cim_sys_path,
                                        AssocClass='CIM_HostedStoragePool',
                                        ResultClass='CIM_StoragePool',
                                        PropertyList=property_list)

        return [p for p in cim_pools if not p["Primordial"]]

    def _new_pool_cim_pool_pros(self):
        """
        Return a list of properties for creating new pool.
        """
        pool_pros = self._property_list_of_id('Pool')
        pool_pros.extend(['ElementName', 'TotalManagedSpace',
                          'RemainingManagedSpace', 'Usage',
                          'OperationalStatus'])
        return pool_pros

    @handle_cim_errors
    def pools(self, search_key=None, search_value=None, flags=0):
        """
        We are basing on "Block Services Package" profile version 1.4 or
        later:
            CIM_ComputerSystem
                 |
                 | (CIM_HostedStoragePool)
                 |
                 v
            CIM_StoragePool
        As 'Block Services Package' is mandatory for 'Array' profile, we
        don't check support status here as startup() already checked 'Array'
        profile.
        """
        rc = []
        cim_pool_pros = self._new_pool_cim_pool_pros()

        cim_sys_pros = self._property_list_of_id("System")
        cim_syss = self._root_cim_syss(cim_sys_pros)

        for cim_sys in cim_syss:
            system_id = self._sys_id(cim_sys)
            for cim_pool in self._cim_pools_of(cim_sys.path, cim_pool_pros):
                # Skip spare storage pool.
                if 'Usage' in cim_pool and \
                   cim_pool['Usage'] == Smis.DMTF_POOL_USAGE_SPARE:
                    continue
                # Skip IBM ArrayPool and ArraySitePool
                # ArrayPool is holding RAID info.
                # ArraySitePool is holding 8 disks. Predefined by array.
                # ArraySite --(1to1 map) --> Array --(1to1 map)--> Rank

                # By design when user get a ELEMENT_TYPE_POOL only pool,
                # user can assume he/she can allocate spaces from that pool
                # to create a new pool with ELEMENT_TYPE_VOLUME or
                # ELEMENT_TYPE_FS ability.

                # If we expose them out, we will have two kind of pools
                # (ArrayPool and ArraySitePool) having element_type &
                # ELEMENT_TYPE_POOL, but none of them can create a
                # ELEMENT_TYPE_VOLUME pool.
                # Only RankPool can create a ELEMENT_TYPE_VOLUME pool.

                # We are trying to hide the detail to provide a simple
                # abstraction.
                if cim_pool.classname == 'IBMTSDS_ArrayPool' or \
                   cim_pool.classname == 'IBMTSDS_ArraySitePool':
                    continue

                pool = self._new_pool(cim_pool, system_id)
                if pool:
                    rc.extend([pool])
                else:
                    raise LsmError(ErrorNumber.LSM_BUG,
                                   "Failed to retrieve pool information " +
                                   "from CIM_StoragePool: %s" % cim_pool.path)
        return search_property(rc, search_key, search_value)

    def _sys_id_of_cim_pool(self, cim_pool):
        """
        Find out the system ID for certain CIM_StoragePool.
        Will return '' if failed.
        """
        sys_pros = self._property_list_of_id('System')
        cim_syss = self._c.Associators(cim_pool.path,
                                       ResultClass='CIM_ComputerSystem',
                                       PropertyList=sys_pros)
        if len(cim_syss) == 1:
            return self._sys_id(cim_syss[0])
        return ''

    @handle_cim_errors
    def _new_pool(self, cim_pool, system_id=''):
        """
        Return a Pool object base on information of cim_pool.
        Assuming cim_pool already holding correct properties.
        """
        if not system_id:
            system_id = self._sys_id_of_cim_pool(cim_pool)

        status_info = ''
        pool_id = self._pool_id(cim_pool)
        name = ''
        total_space = Pool.TOTAL_SPACE_NOT_FOUND
        free_space = Pool.FREE_SPACE_NOT_FOUND
        status = Pool.STATUS_OK
        if 'ElementName' in cim_pool:
            name = cim_pool['ElementName']
        if 'TotalManagedSpace' in cim_pool:
            total_space = cim_pool['TotalManagedSpace']
        if 'RemainingManagedSpace' in cim_pool:
            free_space = cim_pool['RemainingManagedSpace']
        if 'OperationalStatus' in cim_pool:
            status = Smis._pool_status_of(cim_pool)[0]
            status_info = Smis._pool_status_of(cim_pool)[1]

        element_type = self._pool_element_type(cim_pool)

        return Pool(pool_id, name, element_type, total_space, free_space,
                    status, status_info, system_id)

    @staticmethod
    def _cim_sys_2_lsm_sys(cim_sys):
        # In the case of systems we are assuming that the System Name is
        # unique.
        status = System.STATUS_UNKNOWN

        if 'OperationalStatus' in cim_sys:
            for os in cim_sys['OperationalStatus']:
                if os == Smis.SystemOperationalStatus.OK:
                    status |= System.STATUS_OK
                elif os == Smis.SystemOperationalStatus.DEGRADED:
                    status |= System.STATUS_DEGRADED
                elif (os == Smis.SystemOperationalStatus.ERROR or
                      os == Smis.SystemOperationalStatus.STRESSED or
                      os ==
                        Smis.SystemOperationalStatus.NON_RECOVERABLE_ERROR):
                    status |= System.STATUS_ERROR
                elif os == Smis.SystemOperationalStatus.PREDICTIVE_FAILURE:
                    status |= System.STATUS_PREDICTIVE_FAILURE

        return System(cim_sys['Name'], cim_sys['ElementName'], status, '')

    def _cim_sys_pros(self):
        """
        Return a list of properties required to create a LSM System
        """
        cim_sys_pros = self._property_list_of_id('System',
                                                 ['ElementName',
                                                  'OperationalStatus'])
        return cim_sys_pros

    @handle_cim_errors
    def systems(self, flags=0):
        """
        Return the storage arrays accessible from this plug-in at this time

        As 'Block Services Package' is mandatory for 'Array' profile, we
        don't check support status here as startup() already checked 'Array'
        profile.
        """
        cim_sys_pros = self._cim_sys_pros()
        cim_syss = self._root_cim_syss(cim_sys_pros)

        return [Smis._cim_sys_2_lsm_sys(s) for s in cim_syss]

    @handle_cim_errors
    def volume_create(self, pool, volume_name, size_bytes, provisioning,
                      flags=0):
        """
        Create a volume.
        """
        if provisioning != Volume.PROVISION_DEFAULT:
            raise LsmError(ErrorNumber.UNSUPPORTED_PROVISIONING,
                           "Unsupported provisioning")

        # Get the Configuration service for the system we are interested in.
        scs = self._get_class_instance('CIM_StorageConfigurationService',
                                       'SystemName', pool.system_id)
        sp = self._get_cim_instance_by_id('Pool', pool.id)

        in_params = {'ElementName': volume_name,
                     'ElementType': pywbem.Uint16(2),
                     'InPool': sp.path,
                     'Size': pywbem.Uint64(size_bytes)}

        return self._pi("volume_create", Smis.JOB_RETRIEVE_VOLUME,
                        *(self._c.InvokeMethod(
                            'CreateOrModifyElementFromStoragePool',
                            scs.path, **in_params)))

    def _poll(self, msg, job):
        if job:
            while True:
                (s, percent, i) = self.job_status(job)

                if s == JobStatus.INPROGRESS:
                    time.sleep(0.25)
                elif s == JobStatus.COMPLETE:
                    self.job_free(job)
                    return i
                else:
                    raise LsmError(
                        ErrorNumber.PLUGIN_ERROR,
                        msg + ", job error code= " + str(s))

    def _detach(self, vol, sync):
        rs = self._get_class_instance("CIM_ReplicationService", 'SystemName',
                                      vol.system_id, raise_error=False)

        if rs:
            in_params = {'Operation': pywbem.Uint16(8),
                         'Synchronization': sync.path}

            job_id = self._pi("_detach", Smis.JOB_RETRIEVE_NONE,
                              *(self._c.InvokeMethod(
                                  'ModifyReplicaSynchronization', rs.path,
                                  **in_params)))[0]

            self._poll("ModifyReplicaSynchronization, detach", job_id)

    @staticmethod
    def _cim_name_match(a, b):
        if a['DeviceID'] == b['DeviceID'] \
                and a['SystemName'] == b['SystemName'] \
                and a['SystemCreationClassName'] == \
                b['SystemCreationClassName']:
            return True
        else:
            return False

    def _deal_volume_associations(self, vol, lun):
        """
        Check a volume to see if it has any associations with other
        volumes and deal with them.
        """
        lun_path = lun.path

        try:
            ss = self._c.References(lun_path,
                                    ResultClass='CIM_StorageSynchronized')
        except pywbem.CIMError as e:
            if e[0] == pywbem.CIM_ERR_INVALID_CLASS:
                return
            else:
                raise

        if len(ss):
            for s in ss:
                # TODO: Need to see if detach is a supported operation in
                # replication capabilities.
                #
                # TODO: Theory of delete.  Some arrays will automatically
                # detach a clone, check
                # ReplicationServiceCapabilities.GetSupportedFeatures() and
                # look for "Synchronized clone target detaches automatically".
                # If not automatic then detach manually.  However, we have
                # seen arrays that don't report detach automatically that
                # don't need a detach.
                #
                # This code needs to be re-investigated to work with a wide
                # range of array vendors.

                if 'SyncState' in s and 'CopyType' in s:
                    if s['SyncState'] == \
                            Smis.Synchronized.SyncState.SYNCHRONIZED and \
                            (s['CopyType'] != Smis.CopyTypes.UNSYNCASSOC):
                        if 'SyncedElement' in s:
                            item = s['SyncedElement']

                            if Smis._cim_name_match(item, lun_path):
                                self._detach(vol, s)

                        if 'SystemElement' in s:
                            item = s['SystemElement']

                            if Smis._cim_name_match(item, lun_path):
                                self._detach(vol, s)

    @handle_cim_errors
    def volume_delete(self, volume, flags=0):
        """
        Delete a volume
        """
        scs = self._get_class_instance('CIM_StorageConfigurationService',
                                       'SystemName', volume.system_id)
        lun = self._get_cim_instance_by_id('Volume', volume.id)

        self._deal_volume_associations(volume, lun)

        in_params = {'TheElement': lun.path}

        # Delete returns None or Job number
        return self._pi("volume_delete", Smis.JOB_RETRIEVE_NONE,
                        *(self._c.InvokeMethod('ReturnToStoragePool',
                                               scs.path,
                                               **in_params)))[0]

    @handle_cim_errors
    def volume_resize(self, volume, new_size_bytes, flags=0):
        """
        Re-size a volume
        """
        scs = self._get_class_instance('CIM_StorageConfigurationService',
                                       'SystemName', volume.system_id)
        lun = self._get_cim_instance_by_id('Volume', volume.id)

        in_params = {'ElementType': pywbem.Uint16(2),
                     'TheElement': lun.path,
                     'Size': pywbem.Uint64(new_size_bytes)}

        return self._pi("volume_resize", Smis.JOB_RETRIEVE_VOLUME,
                        *(self._c.InvokeMethod(
                            'CreateOrModifyElementFromStoragePool',
                            scs.path, **in_params)))

    def _get_supported_sync_and_mode(self, system_id, rep_type):
        """
        Converts from a library capability to a suitable array capability

        returns a tuple (sync, mode)
        """
        rc = [None, None]

        rs = self._get_class_instance("CIM_ReplicationService", 'SystemName',
                                      system_id, raise_error=False)

        if rs:
            rs_cap = self._c.Associators(
                rs.path,
                AssocClass='CIM_ElementCapabilities',
                ResultClass='CIM_ReplicationServiceCapabilities')[0]

            s_rt = rs_cap['SupportedReplicationTypes']

            if rep_type == Volume.REPLICATE_COPY:
                if self.RepSvc.RepTypes.SYNC_CLONE_LOCAL in s_rt:
                    rc[0] = Smis.SYNC_TYPE_CLONE
                    rc[1] = Smis.CREATE_ELEMENT_REPLICA_MODE_SYNC
                elif self.RepSvc.RepTypes.ASYNC_CLONE_LOCAL in s_rt:
                    rc[0] = Smis.SYNC_TYPE_CLONE
                    rc[1] = Smis.CREATE_ELEMENT_REPLICA_MODE_ASYNC

            elif rep_type == Volume.REPLICATE_MIRROR_ASYNC:
                if self.RepSvc.RepTypes.ASYNC_MIRROR_LOCAL in s_rt:
                    rc[0] = Smis.SYNC_TYPE_MIRROR
                    rc[1] = Smis.CREATE_ELEMENT_REPLICA_MODE_ASYNC

            elif rep_type == Volume.REPLICATE_MIRROR_SYNC:
                if self.RepSvc.RepTypes.SYNC_MIRROR_LOCAL in s_rt:
                    rc[0] = Smis.SYNC_TYPE_MIRROR
                    rc[1] = Smis.CREATE_ELEMENT_REPLICA_MODE_SYNC

            elif rep_type == Volume.REPLICATE_CLONE \
                    or rep_type == Volume.REPLICATE_SNAPSHOT:
                if self.RepSvc.RepTypes.SYNC_CLONE_LOCAL in s_rt:
                    rc[0] = Smis.SYNC_TYPE_SNAPSHOT
                    rc[1] = Smis.CREATE_ELEMENT_REPLICA_MODE_SYNC
                elif self.RepSvc.RepTypes.ASYNC_CLONE_LOCAL in s_rt:
                    rc[0] = Smis.SYNC_TYPE_SNAPSHOT
                    rc[1] = Smis.CREATE_ELEMENT_REPLICA_MODE_ASYNC

        if rc[0] is None:
            raise LsmError(ErrorNumber.NO_SUPPORT,
                           "Replication type not supported")

        return tuple(rc)

    @handle_cim_errors
    def volume_replicate(self, pool, rep_type, volume_src, name, flags=0):
        """
        Replicate a volume
        """
        if rep_type == Volume.REPLICATE_MIRROR_ASYNC \
                or rep_type == Volume.REPLICATE_MIRROR_SYNC:
            raise LsmError(ErrorNumber.NO_SUPPORT, "Mirroring not supported")

        rs = self._get_class_instance("CIM_ReplicationService", 'SystemName',
                                      volume_src.system_id, raise_error=False)

        if pool is not None:
            cim_pool = self._get_cim_instance_by_id('Pool', pool.id)
        else:
            cim_pool = None

        lun = self._get_cim_instance_by_id('Volume', volume_src.id)

        if rs:
            method = 'CreateElementReplica'

            sync, mode = self._get_supported_sync_and_mode(
                volume_src.system_id, rep_type)

            in_params = {'ElementName': name,
                         'SyncType': pywbem.Uint16(sync),
                         #'Mode': pywbem.Uint16(mode),
                         'SourceElement': lun.path,
                         'WaitForCopyState':
                         pywbem.Uint16(Smis.CopyStates.SYNCHRONIZED)}

        else:
            # Check for older support via storage configuration service

            method = 'CreateReplica'

            # Check for storage configuration service
            rs = self._get_class_instance("CIM_StorageConfigurationService",
                                          'SystemName', volume_src.system_id,
                                          raise_error=False)

            ct = Volume.REPLICATE_CLONE
            if rep_type == Volume.REPLICATE_CLONE:
                ct = Smis.CopyTypes.UNSYNCASSOC
            elif rep_type == Volume.REPLICATE_COPY:
                ct = Smis.CopyTypes.UNSYNCUNASSOC
            elif rep_type == Volume.REPLICATE_MIRROR_ASYNC:
                ct = Smis.CopyTypes.ASYNC
            elif rep_type == Volume.REPLICATE_MIRROR_SYNC:
                ct = Smis.CopyTypes.SYNC

            in_params = {'ElementName': name,
                         'CopyType': pywbem.Uint16(ct),
                         'SourceElement': lun.path}
        if rs:

            if cim_pool is not None:
                in_params['TargetPool'] = cim_pool.path

            return self._pi("volume_replicate", Smis.JOB_RETRIEVE_VOLUME,
                            *(self._c.InvokeMethod(method,
                                                   rs.path, **in_params)))

        raise LsmError(ErrorNumber.NO_SUPPORT,
                       "volume-replicate not supported")

    @handle_cim_errors
    def volume_online(self, volume, flags=0):
        return None

    @handle_cim_errors
    def volume_offline(self, volume, flags=0):
        return None

    @handle_cim_errors
    def volume_mask(self, access_group, volume, flags=0):
        """
        Grant access to a volume to an group
        """
        cim_ccs = self._get_class_instance(
            'CIM_ControllerConfigurationService',
            'SystemName', access_group.system_id)
        lun = self._get_cim_instance_by_id('Volume', volume.id, ['Name'])
        spc = self._get_cim_instance_by_id('AccessGroup', access_group.id)

        if not lun:
            raise LsmError(ErrorNumber.NOT_FOUND_VOLUME, "Volume not present")

        if not spc:
            raise LsmError(ErrorNumber.NOT_FOUND_ACCESS_GROUP,
                           "Access group not present")

        da = Smis.EXPOSE_PATHS_DA_READ_WRITE

        in_params = {'LUNames': [lun['Name']],
                     'ProtocolControllers': [spc.path],
                     'DeviceAccesses': [pywbem.Uint16(da)]}

        # Returns None or job id
        return self._pi("access_grant", Smis.JOB_RETRIEVE_NONE,
                        *(self._c.InvokeMethod('ExposePaths', cim_ccs.path,
                                               **in_params)))[0]

    def _wait(self, job):

        status = self.job_status(job)[0]

        while JobStatus.COMPLETE != status:
            time.sleep(0.5)
            status = self.job_status(job)[0]

        if JobStatus.COMPLETE != status:
            raise LsmError(ErrorNumber.PLUGIN_ERROR,
                           "Expected no errors %s %s" % (job, str(status)))

    @handle_cim_errors
    def volume_unmask(self, access_group, volume, flags=0):
        cim_ccs = self._get_class_instance(
            'CIM_ControllerConfigurationService',
            'SystemName', access_group.system_id)
        lun = self._get_cim_instance_by_id('Volume', volume.id, ['Name'])
        spc = self._get_cim_instance_by_id('AccessGroup', access_group.id)

        if not lun:
            raise LsmError(ErrorNumber.NOT_FOUND_VOLUME, "Volume not present")

        if not spc:
            raise LsmError(ErrorNumber.NOT_FOUND_ACCESS_GROUP,
                           "Access group not present")

        hide_params = {'LUNames': [lun['Name']],
                       'ProtocolControllers': [spc.path]}
        return self._pi("HidePaths", Smis.JOB_RETRIEVE_NONE,
                        *(self._c.InvokeMethod('HidePaths', cim_ccs.path,
                                               **hide_params)))[0]

    def _is_access_group(self, cim_ag):
        rc = True
        _SMIS_EMC_ADAPTER_ROLE_MASKING = 'MASK_VIEW'

        if 'EMCAdapterRole' in cim_ag:
            # Currently SNIA does not define LUN mapping.
            # EMC is using their specific way for LUN mapping which
            # expose their frontend ports as a SPC(SCSIProtocolController).
            # which we shall filter out.
            emc_adp_roles = cim_ag['EMCAdapterRole'].split(' ')
            if _SMIS_EMC_ADAPTER_ROLE_MASKING not in emc_adp_roles:
                rc = False
        return rc

    def _cim_ags_of(self, cim_sys, property_list=None):
        """
        Return a list of CIM_SCSIProtocolController.
        Following SNIA SMIS 'Masking and Mapping Profile':
            CIM_ComputerSystem
                |
                | CIM_HostedService
                v
            CIM_ControllerConfigurationService
                |
                | CIM_ConcreteDependency
                v
            CIM_SCSIProtocolController
        """
        cim_ccss_path = []
        rc_cim_ags = []

        if property_list is None:
            property_list = []

        try:
            cim_ccss_path = self._c.AssociatorNames(
                cim_sys.path,
                AssocClass='CIM_HostedService',
                ResultClass='CIM_ControllerConfigurationService')
        except CIMError as ce:
            error_code = tuple(ce)[0]
            if error_code == pywbem.CIM_ERR_INVALID_CLASS or \
                    error_code == pywbem.CIM_ERR_INVALID_PARAMETER:
                raise LsmError(ErrorNumber.NO_SUPPORT,
                               'AccessGroup is not supported ' +
                               'by this array')
        cim_ccs_path = None
        if len(cim_ccss_path) == 1:
            cim_ccs_path = cim_ccss_path[0]
        elif len(cim_ccss_path) == 0:
            raise LsmError(ErrorNumber.NO_SUPPORT,
                           'AccessGroup is not supported by this array')
        else:
            raise LsmError(ErrorNumber.LSM_BUG,
                           "Got %d instance of " % len(cim_ccss_path) +
                           "ControllerConfigurationService from %s" %
                           cim_sys.path + " in _cim_ags_of()")
        cim_ags = self._c.Associators(
            cim_ccs_path,
            AssocClass='CIM_ConcreteDependency',
            ResultClass='CIM_SCSIProtocolController',
            PropertyList=property_list)
        for cim_ag in cim_ags:
            if self._is_access_group(cim_ag):
                rc_cim_ags.append(cim_ag)
        return rc_cim_ags

    def _cim_init_of(self, cim_ag, property_list=None):
        """
        Take CIM_SCSIProtocolController and return a list of
        CIM_StorageHardwareID, both are CIMInstance.
        Two ways to get StorageHardwareID from SCSIProtocolController:
         * Method A (defined in SNIA SMIS 1.6):
              CIM_SCSIProtocolController
                      |
                      | CIM_AssociatedPrivilege
                      v
              CIM_StorageHardwareID

         * Method B (defined in SNIA SMIS 1.3, 1.4, 1.5 and 1.6):
              CIM_SCSIProtocolController
                      |
                      | CIM_AuthorizedTarget
                      v
              CIM_AuthorizedPrivilege
                      |
                      | CIM_AuthorizedSubject
                      v
              CIM_StorageHardwareID

        Method A defined in SNIA SMIS 1.6 deprecated the Method B and Method A
        saved 1 query which provide better performance.
        Hence we try method A.
        Maybe someday, we will stop trying after knowing array's supported
        SMIS version.
        """
        cim_inits = []
        if property_list is None:
            property_list = []

        if (not self.fallback_mode and
            self._profile_is_supported(SNIA.MASK_PROFILE,
                                       SNIA.SMIS_SPEC_VER_1_6,
                                       strict=False,
                                       raise_error=False)):
            return self._c.Associators(
                cim_ag.path,
                AssocClass='CIM_AssociatedPrivilege',
                ResultClass='CIM_StorageHardwareID',
                PropertyList=property_list)
        else:
            cim_aps_path = self._c.AssociatorNames(
                cim_ag.path,
                AssocClass='CIM_AuthorizedTarget',
                ResultClass='CIM_AuthorizedPrivilege')
            for cim_ap_path in cim_aps_path:
                cim_inits.extend(self._c.Associators(
                    cim_ap_path,
                    AssocClass='CIM_AuthorizedSubject',
                    ResultClass='CIM_StorageHardwareID',
                    PropertyList=property_list))
            return cim_inits

    @handle_cim_errors
    def volumes_accessible_by_access_group(self, access_group, flags=0):
        g = self._get_class_instance('CIM_SCSIProtocolController', 'DeviceID',
                                     access_group.id)
        if g:
            logical_units = self._c.Associators(
                g.path, AssocClass='CIM_ProtocolControllerForUnit')
            return [self._new_vol(v) for v in logical_units]
        else:
            raise LsmError(
                ErrorNumber.PLUGIN_ERROR,
                'Error: access group %s does not exist!' % access_group.id)

    @handle_cim_errors
    def access_groups_granted_to_volume(self, volume, flags=0):
        vol = self._get_cim_instance_by_id('Volume', volume.id)

        if vol:
            cim_ags = self._c.Associators(
                vol.path,
                AssocClass='CIM_ProtocolControllerForUnit',
                ResultClass='CIM_SCSIProtocolController')

            access_groups = []
            for cim_ag in cim_ags:
                if self._is_access_group(cim_ag):
                    access_groups.extend([self._cim_ag_to_lsm(cim_ag)])

            return access_groups
        else:
            raise LsmError(
                ErrorNumber.PLUGIN_ERROR,
                'Error: access group %s does not exist!' % volume.id)

    @handle_cim_errors
    def access_groups(self, search_key=None, search_value=None, flags=0):
        if not self.fallback_mode:
            self._profile_is_supported(SNIA.MASK_PROFILE,
                                       SNIA.SMIS_SPEC_VER_1_4,
                                       strict=False,
                                       raise_error=True)
        rc = []
        cim_ag_pros = self._cim_ag_pros()
        cim_sys_pros = self._property_list_of_id('System')
        cim_syss = self._root_cim_syss(cim_sys_pros)
        for cim_sys in cim_syss:
            system_id = self._sys_id(cim_sys)
            cim_ags = self._cim_ags_of(cim_sys, cim_ag_pros)
            rc.extend(
                list(self._cim_ag_to_lsm(cim_ag, system_id)
                     for cim_ag in cim_ags))

        return search_property(rc, search_key, search_value)

    def _initiator_create(self, cim_sys_path, init_id, dmtf_id_type):
        """
        Create a CIM_StorageHardwareID.
        Raise error if failed. Return if pass.
        """
        cim_hw_srvs = self._c.AssociatorNames(
            cim_sys_path,
            ResultClass='CIM_StorageHardwareIDManagementService',
            AssocClass='CIM_HostedService')
        if len(cim_hw_srvs) == 0:
            raise LsmError(ErrorNumber.NO_SUPPORT,
                           "Target SMI-S provider does not support "
                           "access_group_initiator_add(): No "
                           "CIM_StorageHardwareIDManagementService to create"
                           "new initiator")
        if len(cim_hw_srvs) != 1:
            raise LsmError(ErrorNumber.LSM_BUG,
                           "_initiator_create(): Got more than one "
                           "CIM_StorageHardwareIDManagementService")

        in_params = {'StorageID': init_id,
                     'IDType': pywbem.Uint16(dmtf_id_type)}

        (rc, out) = self._c.InvokeMethod('CreateStorageHardwareID',
                                         cim_hw_srvs[0], **in_params)
        if not rc:
            return

        # Ideally, we should handle CIM Error here in stead of raise
        # LSM_BUG error. Let's wait user report on bug.
        raise LsmError(ErrorNumber.LSM_BUG,
                       'Error on _initiator_create(): rc: "%s", out: "%s"'
                       % (str(rc), out))

    @handle_cim_errors
    def access_group_initiator_add(self, access_group, init_id, init_type,
                                   flags=0):
        # CIM_StorageHardwareIDManagementService.CreateStorageHardwareID()
        # is mandatory since 1.4rev6
        if not self.fallback_mode:
            self._profile_is_supported(SNIA.MASK_PROFILE,
                                       SNIA.SMIS_SPEC_VER_1_4,
                                       strict=False,
                                       raise_error=True)

        cim_sys = self._get_cim_instance_by_id('System',
                                               access_group.system_id)

        # Check to see if we have this initiator already, if we don't create
        # it and then add to the view.
        if self._get_cim_instance_by_id(
                'Initiator', init_id, raise_error=False) is None:
            dmtf_id_type = _lsm_init_type_to_dmtf(init_type)
            if dmtf_id_type is None:
                raise LsmError(ErrorNumber.NO_SUPPORT,
                               "SMI-S Plugin does not support init_type %d"
                               % init_type)
            self._initiator_create(cim_sys.path, init_id, dmtf_id_type)

        cim_ag = self._get_cim_instance_by_id('AccessGroup', access_group.id)
        cim_ccs = self._get_class_instance(
            'CIM_ControllerConfigurationService',
            'SystemName', access_group.system_id)

        in_params = {'InitiatorPortIDs': [init_id],
                     'ProtocolControllers': [cim_ag.path]}

        # Returns None or job id
        return self._pi("access_group_initiator_add", Smis.JOB_RETRIEVE_NONE,
                        *(self._c.InvokeMethod('ExposePaths', cim_ccs.path,
                                               **in_params)))[0]

    @handle_cim_errors
    def access_group_initiator_delete(self, access_group, init_id, flags=0):
        cim_ag = self._get_cim_instance_by_id('AccessGroup', access_group.id)
        cim_ccs = self._get_class_instance(
            'CIM_ControllerConfigurationService',
            'SystemName', access_group.system_id)

        hide_params = {'InitiatorPortIDs': [init_id],
                       'ProtocolControllers': [cim_ag.path]}
        return self._pi("HidePaths", Smis.JOB_RETRIEVE_NONE,
                        *(self._c.InvokeMethod('HidePaths', cim_ccs.path,
                                               **hide_params)))[0]

    @handle_cim_errors
    def job_free(self, job_id, flags=0):
        """
        Frees the resources given a job number.
        """
        cim_job = self._get_cim_instance_by_id('Job', job_id,
                                               ['DeleteOnCompletion'])

        # See if we should delete the job
        if not cim_job['DeleteOnCompletion']:
            try:
                self._c.DeleteInstance(cim_job.path)
            except CIMError:
                pass

    def _enumerate(self, class_name, property_list=None):
        """
        Please do the filter of "sytems=" in URI by yourself.
        """
        if len(self.all_vendor_namespaces) == 0:
            # We need to find out the vendor spaces.
            # We do it here to save plugin_register() time.
            # Only non-fallback mode can goes there.
            cim_syss = self._root_cim_syss()
            all_vendor_namespaces = []
            for cim_sys in cim_syss:
                if cim_sys.path.namespace not in all_vendor_namespaces:
                    all_vendor_namespaces.extend([cim_sys.path.namespace])
            self.all_vendor_namespaces = all_vendor_namespaces
        rc = []
        e_args = dict(LocalOnly=False)
        if property_list is not None:
            e_args['PropertyList'] = property_list
        for vendor_namespace in self.all_vendor_namespaces:
            rc.extend(self._c.EnumerateInstances(class_name, vendor_namespace,
                                                 **e_args))
        return rc

    @handle_cim_errors
    def disks(self, search_key=None, search_value=None, flags=0):
        """
        return all object of data.Disk.
        We are using "Disk Drive Lite Subprofile" v1.4 of SNIA SMI-S for these
        classes to create LSM Disk:
            CIM_PhysicalPackage
            CIM_DiskDrive
            CIM_StorageExtent (Primordial)
        Due to 'Multiple Computer System' profile, disks might assocated to
        sub ComputerSystem. To improve profromance of listing disks, we will
        use EnumerateInstances(). Which means we have to filter the results
        by ourself in case URI contain 'system=xxx'.
        """
        rc = []
        if not self.fallback_mode:
            self._profile_is_supported(SNIA.DISK_LITE_PROFILE,
                                       SNIA.SMIS_SPEC_VER_1_4,
                                       strict=False,
                                       raise_error=True)
        cim_disk_pros = Smis._new_disk_cim_disk_pros(flags)
        cim_disks = self._enumerate('CIM_DiskDrive', cim_disk_pros)
        for cim_disk in cim_disks:
            if self.system_list:
                if self._sys_id_child(cim_disk) not in self.system_list:
                    continue
            cim_ext_pros = Smis._new_disk_cim_ext_pros(flags)
            cim_ext = self._pri_cim_ext_of_cim_disk(cim_disk.path,
                                                    cim_ext_pros)

            rc.extend([self._new_disk(cim_disk, cim_ext)])
        return search_property(rc, search_key, search_value)

    @staticmethod
    def _new_disk_cim_disk_pros(flag=0):
        """
        Return all CIM_DiskDrive Properties needed to create a Disk object.
        """
        pros = ['OperationalStatus', 'Name', 'SystemName',
                'Caption', 'InterconnectType', 'DiskType']
        return pros

    @staticmethod
    def _new_disk_cim_ext_pros(flag=0):
        """
        Return all CIM_StorageExtent Properties needed to create a Disk
        object.
        """
        return ['BlockSize', 'NumberOfBlocks']

    @staticmethod
    def _disk_status_of(cim_disk):
        """
        Converting CIM_StorageDisk['OperationalStatus'] LSM Disk.status.
        This might change since OperationalStatus does not provide enough
        information.
        Return (status, status_info)
        """
        status = Disk.STATUS_UNKNOWN
        status_info = ''
        dmtf_statuses = cim_disk['OperationalStatus']
        for dmtf_status in dmtf_statuses:
            if dmtf_status in Smis._DMTF_STAUTS_TO_DISK_STATUS.keys():
                lsm_status = Smis._DMTF_STAUTS_TO_DISK_STATUS[dmtf_status]
                if status == Disk.STATUS_UNKNOWN:
                    status = lsm_status
                else:
                    status |= lsm_status
            if dmtf_status in Smis._DMTF_STAUTS_TO_DISK_STATUS_INFO.keys():
                status_info = txt_a(
                    status_info,
                    Smis._DMTF_STAUTS_TO_DISK_STATUS_INFO[dmtf_status])
        return (status, status_info)

    def _new_disk(self, cim_disk, cim_ext):
        """
        Takes a CIM_DiskDrive and CIM_StorageExtent, returns a lsm Disk
        Assuming cim_disk and cim_ext already contained the correct
        properties.
        """
        status = Disk.STATUS_UNKNOWN
        name = ''
        block_size = Disk.BLOCK_SIZE_NOT_FOUND
        num_of_block = Disk.BLOCK_COUNT_NOT_FOUND
        disk_type = Disk.DISK_TYPE_UNKNOWN
        status_info = ''
        sys_id = self._sys_id_child(cim_disk)

        # These are mandatory
        # we do not check whether they follow the SNIA standard.
        if 'OperationalStatus' in cim_disk:
            (status, status_info) = Smis._disk_status_of(cim_disk)
        if 'Name' in cim_disk:
            name = cim_disk["Name"]
        if 'BlockSize' in cim_ext:
            block_size = cim_ext['BlockSize']
        if 'NumberOfBlocks' in cim_ext:
            num_of_block = cim_ext['NumberOfBlocks']

        # SNIA SMI-S 1.4 or even 1.6 does not define anyway to find out disk
        # type.
        # Currently, EMC is following DMTF define to do so.
        if 'InterconnectType' in cim_disk:  # DMTF 2.31 CIM_DiskDrive
            disk_type = cim_disk['InterconnectType']
            if 'Caption' in cim_disk:
                # EMC VNX introduced NL_SAS disk.
                if cim_disk['Caption'] == 'NL_SAS':
                    disk_type = Disk.DISK_TYPE_NL_SAS

        if disk_type == Disk.DISK_TYPE_UNKNOWN and 'DiskType' in cim_disk:
            disk_type = \
                Smis.dmtf_disk_type_2_lsm_disk_type(cim_disk['DiskType'])

        # LSI way for checking disk type
        if not disk_type and cim_disk.classname == 'LSIESG_DiskDrive':
            cim_pes = self._c.Associators(
                cim_disk.path,
                AssocClass='CIM_SAPAvailableForElement',
                ResultClass='CIM_ProtocolEndpoint',
                PropertyList=['CreationClassName'])
            if cim_pes and cim_pes[0]:
                if 'CreationClassName' in cim_pes[0]:
                    ccn = cim_pes[0]['CreationClassName']
                    if ccn == 'LSIESG_TargetSATAProtocolEndpoint':
                        disk_type = Disk.DISK_TYPE_SATA
                    if ccn == 'LSIESG_TargetSASProtocolEndpoint':
                        disk_type = Disk.DISK_TYPE_SAS

        new_disk = Disk(self._disk_id(cim_disk), name, disk_type, block_size,
                        num_of_block, status, sys_id)

        return new_disk

    def _pri_cim_ext_of_cim_disk(self, cim_disk_path, property_list=None):
        """
        Usage:
            Find out the Primordial CIM_StorageExtent of CIM_DiskDrive
            In SNIA SMI-S 1.4 rev.6 Block book, section 11.1.1 'Base Model'
            quote:
            A disk drive is modeled as a single MediaAccessDevice (DiskDrive)
            That shall be linked to a single StorageExtent (representing the
            storage in the drive) by a MediaPresent association. The
            StorageExtent class represents the storage of the drive and
            contains its size.
        Parameter:
            cim_disk_path   # CIM_InstanceName of CIM_DiskDrive
            property_list   # a List of properties needed on returned
                            # CIM_StorageExtent
        Returns:
            cim_pri_ext     # The CIM_Instance of Primordial CIM_StorageExtent
        Exceptions:
            LsmError
                ErrorNumber.LSM_BUG  # Failed to find out pri cim_ext
        """
        if property_list is None:
            property_list = ['Primordial']
        else:
            property_list = _merge_list(property_list, ['Primordial'])

        cim_exts = self._c.Associators(
            cim_disk_path,
            AssocClass='CIM_MediaPresent',
            ResultClass='CIM_StorageExtent',
            PropertyList=property_list)
        cim_exts = [p for p in cim_exts if p["Primordial"]]
        if cim_exts and cim_exts[0]:
            # As SNIA commanded, only _ONE_ Primordial CIM_StorageExtent for
            # each CIM_DiskDrive
            return cim_exts[0]
        else:
            raise LsmError(ErrorNumber.LSM_BUG,
                           "Failed to find out Primordial " +
                           "CIM_StorageExtent for CIM_DiskDrive %s " %
                           cim_disk_path)

    @staticmethod
    def _pool_status_of(cim_pool):
        """
        Converting CIM_StoragePool['OperationalStatus'] LSM Pool.status.
        This might change since OperationalStatus does not provide enough
        information.
        Return (status, status_info)
        """
        status = Pool.STATUS_UNKNOWN
        status_info = ''
        dmtf_statuses = cim_pool['OperationalStatus']
        for dmtf_status in dmtf_statuses:
            if dmtf_status in Smis._DMTF_STAUTS_TO_POOL_STATUS.keys():

                lsm_status = Smis._DMTF_STAUTS_TO_POOL_STATUS[dmtf_status]
                if status == Pool.STATUS_UNKNOWN:
                    status = lsm_status
                else:
                    status |= lsm_status
            if dmtf_status in Smis._DMTF_STAUTS_TO_POOL_STATUS_INFO.keys():
                status_info = txt_a(
                    status_info,
                    Smis._DMTF_STAUTS_TO_POOL_STATUS_INFO[dmtf_status])
        return (status, status_info)

    def _find_out_bottom_cexts(self, cim_pool_path, pros_list=None):
        """
        This is based on 'Extent Composition' subprofile.
        CIM_StoragePool can based on several CIM_CompositeExtent with several
        level. We will find out the bottom level CIM_CompositeExtent.
        This is how we traverse down:
                CIM_StoragePool
                      ^
                      | GroupComponent
                      |
                      | CIM_ConcreteComponent/CIM_AssociatedComponentExtent
                      |     |-> deprecated in SMI-S 1.5rev4 by ---^
                      |
                      | PartComponent
                      v
                CIM_CompositeExtent     # The rest traverse was handle by
                      ^                 # _traverse_cext()
                      | GroupComponent
                      |
                      | CIM_BasedOn
                      |
                      | PartComponent
                      v
                CIM_CompositeExtent
                      .
                      .
                      .
        Will return a list of CIMInstance of CIM_CompositeExtent.
        Mid-level CIM_CompositeExtent will not included.
        If nothing found, return []
        """
        if pros_list is None:
            pros_list = []
        bottom_cim_cexts = []
        try:
            cim_cexts = self._c.Associators(
                cim_pool_path,
                AssocClass='CIM_AssociatedComponentExtent',
                Role='GroupComponent',
                ResultRole='PartComponent',
                ResultClass='CIM_CompositeExtent',
                PropertyList=pros_list)
        except CIMError as ce:
            error_code = tuple(ce)[0]
            if error_code == pywbem.CIM_ERR_INVALID_CLASS or \
               error_code == pywbem.CIM_ERR_INVALID_PARAMETER:
                # Not support SMIS 1.5, using 1.4 way.
                cim_cexts = self._c.Associators(
                    cim_pool_path,
                    AssocClass='CIM_ConcreteComponent',
                    Role='GroupComponent',
                    ResultRole='PartComponent',
                    ResultClass='CIM_CompositeExtent',
                    PropertyList=pros_list)
            else:
                raise
        if cim_pool_path.classname == 'LSIESG_StoragePool':
            # LSI does not report error on CIM_AssociatedComponentExtent
            # But they don't support it.
            cim_cexts = self._c.Associators(
                cim_pool_path,
                AssocClass='CIM_ConcreteComponent',
                Role='GroupComponent',
                ResultRole='PartComponent',
                ResultClass='CIM_CompositeExtent',
                PropertyList=pros_list)

        if len(cim_cexts) == 0:
            return []
        for cim_cext in cim_cexts:
            tmp_cim_cexts = self._traverse_cext(cim_cext.path, pros_list)
            if len(tmp_cim_cexts) == 0:
                # already at the bottom level
                bottom_cim_cexts.extend([cim_cext])
            else:
                bottom_cim_cexts.extend(tmp_cim_cexts)
        return bottom_cim_cexts

    def _traverse_cext(self, cim_cext_path, pros_list=None):
        """
        Using this procedure to find out the bottom level CIM_CompositeExtent.
                CIM_CompositeExtent
                      ^
                      | GroupComponent
                      |
                      | CIM_BasedOn
                      |
                      | PartComponent
                      v
                CIM_CompositeExtent
                      .
                      .
                      .
        Will return a list of CIMInstance of CIM_CompositeExtent.
        Mid-level CIM_CompositeExtent will not included.
        If nothing found, return []
        """
        if pros_list is None:
            pros_list = []
        cim_sub_cexts = self._c.Associators(
            cim_cext_path,
            AssocClass='CIM_BasedOn',
            ResultClass='CIM_CompositeExtent',
            Role='GroupComponent',
            ResultRole='PartComponent',
            PropertyList=pros_list)
        if len(cim_sub_cexts) == 0:
            return []
        cim_bottom_cexts = []
        for cim_sub_cext in cim_sub_cexts:
            tmp_cim_bottom_cexts = self._traverse_cext(cim_sub_cext.path,
                                                       pros_list)
            if len(tmp_cim_bottom_cexts) == 0:
                cim_bottom_cexts.extend([cim_sub_cext])
            else:
                cim_bottom_cexts.extend(tmp_cim_bottom_cexts)
        return cim_bottom_cexts

    def _traverse_cext_2_pri_ext(self, cim_cext_path, pros_list=None):
        """
        Using this procedure to find out the member disks of
        CIM_CompositeExtent:
                CIM_CompositeExtent
                      ^
                      | Dependent
                      |
                      | CIM_BasedOn
                      |
                      | Antecedent
                      v
                CIM_StorageExtent (Concrete)
                      ^
                      | Dependent
                      |
                      | CIM_BasedOn
                      |
                      | Antecedent
                      v
                CIM_StorageExtent (Concrete)
                      .
                      .
                      .
                CIM_StorageExtent (Primordial)
        """
        if pros_list is None:
            pros_list = []
        if 'Primordial' not in pros_list:
            pros_list.extend(['Primordial'])
        cim_sub_exts = self._c.Associators(
            cim_cext_path,
            AssocClass='CIM_BasedOn',
            ResultClass='CIM_StorageExtent',
            Role='Dependent',
            ResultRole='Antecedent',
            PropertyList=pros_list)
        cim_pri_exts = []
        for cim_sub_ext in cim_sub_exts:
            if cim_sub_ext['Primordial']:
                cim_pri_exts.extend([cim_sub_ext])
            else:
                cim_pri_exts.extend(
                    self._traverse_cext_2_pri_ext(cim_sub_ext.path))
        return cim_pri_exts

    def _cim_disk_of_pri_ext(self, cim_pri_ext_path, pros_list=None):
        """
        Follow this procedure to find out CIM_DiskDrive from Primordial
        CIM_StorageExtent:
                CIM_StorageExtent (Primordial)
                      ^
                      |
                      | CIM_MediaPresent
                      |
                      v
                CIM_DiskDrive
        """
        if pros_list is None:
            pros_list = []
        cim_disks = self._c.Associators(
            cim_pri_ext_path,
            AssocClass='CIM_MediaPresent',
            ResultClass='CIM_DiskDrive',
            PropertyList=pros_list)
        if len(cim_disks) == 1:
            return cim_disks[0]
        elif len(cim_disks) == 2:
            return None
        else:
            raise LsmError(ErrorNumber.LSM_BUG,
                           "Found two or more CIM_DiskDrive associated to " +
                           "requested CIM_StorageExtent %s" %
                           cim_pri_ext_path)

    def _pool_element_type(self, cim_pool):

        element_type = 0

        # check whether current pool support create volume or not.
        cim_sccs = self._c.Associators(
            cim_pool.path,
            AssocClass='CIM_ElementCapabilities',
            ResultClass='CIM_StorageConfigurationCapabilities',
            PropertyList=['SupportedStorageElementFeatures',
                          'SupportedStorageElementTypes'])
        # Associate StorageConfigurationCapabilities to StoragePool
        # is experimental in SNIA 1.6rev4, Block Book PDF Page 68.
        # Section 5.1.6 StoragePool, StorageVolume and LogicalDisk
        # Manipulation, Figure 9 - Capabilities Specific to a StoragePool
        if len(cim_sccs) == 1:
            cim_scc = cim_sccs[0]
            if 'SupportedStorageElementFeatures' in cim_scc and \
                Smis.DMTF_SUPPORT_VOL_CREATE in \
                    cim_scc['SupportedStorageElementFeatures']:
                element_type = Pool.ELEMENT_TYPE_VOLUME
        else:
            # IBM DS 8000 does not support StorageConfigurationCapabilities
            # per pool yet. They has been informed. Before fix, use a quick
            # workaround.
            # TODO: Currently, we don't have a way to detect
            #       Pool.ELEMENT_TYPE_POOL
            #       but based on knowing definition of each vendor.
            if cim_pool.classname == 'IBMTSDS_VirtualPool' or \
               cim_pool.classname == 'IBMTSDS_ExtentPool':
                element_type = Pool.ELEMENT_TYPE_VOLUME
            elif cim_pool.classname == 'IBMTSDS_RankPool':
                element_type = Pool.ELEMENT_TYPE_POOL
            elif cim_pool.classname == 'LSIESG_StoragePool':
                element_type = Pool.ELEMENT_TYPE_VOLUME

        if 'Usage' in cim_pool:
            if cim_pool['Usage'] == Smis.DMTF_POOL_USAGE_DELTA:
                element_type = Pool.ELEMENT_TYPE_DELTA
            if cim_pool['Usage'] == 2:
                element_type = Pool.ELEMENT_TYPE_VOLUME

        return element_type

    def _pool_opt_data(self, cim_pool):
        """
        Usage:
            Update Pool object with optional data found in cim_pool.
            The CIMInstance cim_pool was supposed to hold all optional data.
            So that we save 1 SMI-S query.
            No matter we found any info or not, we still return the unknown
            filler, with this, we can make sure return object are containing
            same order/length of column_data().
        Parameter:
            cim_pool        # CIMInstance of CIM_StoragePool
        Returns:
            opt_pro_dict    # dict containing optional properties
        Exceptions:
            NONE
        """
        opt_pro_dict = {
            'thinp_type': Pool.THINP_TYPE_UNKNOWN,
            'raid_type': Pool.RAID_TYPE_UNKNOWN,
            'member_type': Pool.MEMBER_TYPE_UNKNOWN,
            'member_ids': [],
            'element_type': Pool.ELEMENT_TYPE_UNKNOWN,
        }

        # check whether current pool support create volume or not.
        cim_sccs = self._c.Associators(
            cim_pool.path,
            AssocClass='CIM_ElementCapabilities',
            ResultClass='CIM_StorageConfigurationCapabilities',
            PropertyList=['SupportedStorageElementFeatures',
                          'SupportedStorageElementTypes'])
        # Associate StorageConfigurationCapabilities to StoragePool
        # is experimental in SNIA 1.6rev4, Block Book PDF Page 68.
        # Section 5.1.6 StoragePool, StorageVolume and LogicalDisk
        # Manipulation, Figure 9 - Capabilities Specific to a StoragePool
        if len(cim_sccs) == 1:
            cim_scc = cim_sccs[0]
            if 'SupportedStorageElementFeatures' in cim_scc and \
                Smis.DMTF_SUPPORT_VOL_CREATE in \
                    cim_scc['SupportedStorageElementFeatures']:
                opt_pro_dict['element_type'] = Pool.ELEMENT_TYPE_VOLUME
            # When certain Pool can create ThinlyProvisionedStorageVolume,
            # we mark it as Thin Pool.
            if 'SupportedStorageElementTypes' in cim_scc:
                dmtf_element_types = cim_scc['SupportedStorageElementTypes']
                if Smis.DMTF_ELEMENT_THIN_VOLUME in dmtf_element_types:
                    opt_pro_dict['thinp_type'] = Pool.THINP_TYPE_THIN
                else:
                    opt_pro_dict['thinp_type'] = Pool.THINP_TYPE_THICK
        else:
            # IBM DS 8000 does not support StorageConfigurationCapabilities
            # per pool yet. They has been informed. Before fix, use a quick
            # workaround.
            # TODO: Currently, we don't have a way to detect
            #       Pool.ELEMENT_TYPE_POOL
            #       but based on knowing definition of each vendor.
            if cim_pool.classname == 'IBMTSDS_VirtualPool' or \
               cim_pool.classname == 'IBMTSDS_ExtentPool':
                opt_pro_dict['element_type'] = Pool.ELEMENT_TYPE_VOLUME
            elif cim_pool.classname == 'IBMTSDS_RankPool':
                opt_pro_dict['element_type'] = Pool.ELEMENT_TYPE_POOL
            elif cim_pool.classname == 'LSIESG_StoragePool':
                opt_pro_dict['element_type'] = Pool.ELEMENT_TYPE_VOLUME
                opt_pro_dict['thinp_type'] = Pool.THINP_TYPE_THICK

        pool_id_pros = self._property_list_of_id('Pool', ['Primordial'])
        # We use some blacklist here to speed up by skipping unnecessary
        # parent pool checking.
        # These class are known as Disk Pool, no need to waste time on
        # checking 'Pool over Pool' layout.
        if cim_pool.classname == 'Clar_UnifiedStoragePool' or \
           cim_pool.classname == 'IBMTSDS_RankPool' or \
           cim_pool.classname == 'LSIESG_StoragePool' or \
           cim_pool.classname == 'ONTAP_ConcretePool':
            pass
        else:
            cim_parent_pools = self._c.Associators(
                cim_pool.path,
                AssocClass='CIM_AllocatedFromStoragePool',
                Role='Dependent',
                ResultRole='Antecedent',
                ResultClass='CIM_StoragePool',
                PropertyList=pool_id_pros)
            for cim_parent_pool in cim_parent_pools:
                if not cim_parent_pool['Primordial']:
                    opt_pro_dict['member_type'] = Pool.MEMBER_TYPE_POOL
                    opt_pro_dict['member_ids'].extend(
                        [self._pool_id(cim_parent_pool)])

        raid_pros = self._raid_type_pros()
        cim_cexts = []
        # We skip disk member checking on VMAX due to bad performance.
        if cim_pool.classname != 'Symm_DeviceStoragePool':
            cim_cexts = self._find_out_bottom_cexts(cim_pool.path, raid_pros)
        raid_type = None
        for cim_cext in cim_cexts:
            cur_raid_type = self._raid_type_of(cim_cext)

            if (raid_type is not None) and cur_raid_type != raid_type:
                raid_type = Pool.RAID_TYPE_MIXED
            else:
                raid_type = cur_raid_type

            if opt_pro_dict['member_type'] == Pool.MEMBER_TYPE_POOL:
                # we already know current pool is based on pool or volume.
                # skipping disk member traverse walk.
                continue

            # TODO: Current way consume too much time(too many SMIS call).
            #       SNIA current standard (1.6rev4) does not have any better
            #       way for disk members querying.
            cim_pri_exts = self._traverse_cext_2_pri_ext(cim_cext.path)
            cim_disks = []
            disk_id_pros = self._property_list_of_id('Disk')
            for cim_pri_ext in cim_pri_exts:
                cim_disk = self._cim_disk_of_pri_ext(cim_pri_ext.path,
                                                     disk_id_pros)
                if cim_disk:
                    cim_disks.extend([cim_disk])
            if len(cim_disks) > 0:
                cur_member_ids = []
                for cim_disk in cim_disks:
                    cur_member_ids.extend([self._disk_id(cim_disk)])

                opt_pro_dict['member_type'] = Pool.MEMBER_TYPE_DISK
                opt_pro_dict['member_ids'].extend(cur_member_ids)

        if raid_type is not None:
            opt_pro_dict['raid_type'] = raid_type

        return opt_pro_dict

    @staticmethod
    def _raid_type_pros():
        """
        Return a list of properties needed to detect RAID type from
        CIM_StorageExtent.
        """
        return ['DataRedundancy', 'PackageRedundancy',
                'NoSinglePointOfFailure', 'ExtentStripeLength']

    @staticmethod
    def _raid_type_of(cim_ext):
        """
        Take CIM_CompositePool to check the RAID type of it.
        Only check the up-first level of RAID, we does not nested down.
        For example, when got a RAID 1 CIM_CompositePool, we return
            Pool.RAID_TYPE_RAID1
        If failed to detect the RAID level, will return:
            Pool.RAID_TYPE_UNKNOWN
        Since this is a private method, we do not check whether cim_ext is
        valid or not.
        Make sure you have all properties listed in _raid_type_pros()
        # TODO: to support RAID 3 and RAID 4 level.
        #       RAID 3/4 could be checked via
        #       CIM_StorageSetting['ParityLayout']
        #       RAID 3: stripesize is 512 (ExtentStripeLength == 1)
        #       RAID 4: stripesize is 512 * (disk_count -1)
        #
        #       Problem is: there is no SNIA spec said CIM_StorageSetting
        #       should associate to CIM_CompositeExtent.
        #       Since RAID 3/4 is rare in market, low priority.
        """
        if not cim_ext:
            return Pool.RAID_TYPE_UNKNOWN
        if 'DataRedundancy' not in cim_ext or \
           'PackageRedundancy' not in cim_ext or \
           'NoSinglePointOfFailure' not in cim_ext or \
           'ExtentStripeLength' not in cim_ext:
            return Pool.RAID_TYPE_UNKNOWN

        # DataRedundancy:
        # Number of complete copies of data currently maintained.
        data_redundancy = cim_ext['DataRedundancy']
        # PackageRedundancy:
        # How many physical packages can currently fail without data loss.
        # For example, in the storage domain, this might be disk spindles.
        pack_redundancy = cim_ext['PackageRedundancy']
        # NoSinglePointOfFailure:
        # Indicates whether or not there exists no single point of
        # failure.
        no_spof = cim_ext['NoSinglePointOfFailure']

        # ExtentStripeLength:
        # Number of contiguous underlying StorageExtents counted before
        # looping back to the first underlying StorageExtent of the
        # current stripe. It is the number of StorageExtents forming the
        # user data stripe.
        stripe_len = cim_ext['ExtentStripeLength']

        # determine the RAID type as SNIA document require.
        # JBOD
        if ((data_redundancy == 1) and
           (pack_redundancy == 0) and
           (not no_spof) and
           (stripe_len == 1)):
            return Pool.RAID_TYPE_JBOD
        # RAID 0
        elif ((data_redundancy == 1) and
             (pack_redundancy == 0) and
             (not no_spof) and
             (stripe_len >= 1)):
            return Pool.RAID_TYPE_RAID0
        # RAID 1
        elif ((data_redundancy == 2) and
             (pack_redundancy == 1) and
             (no_spof) and
             (stripe_len == 1)):
            return Pool.RAID_TYPE_RAID1
        # RAID 5
        elif ((data_redundancy == 1) and
             (pack_redundancy == 1) and
             (no_spof) and
             (stripe_len >= 1)):
            return Pool.RAID_TYPE_RAID5
        # RAID 6
        elif ((data_redundancy == 1) and
             (pack_redundancy == 2) and
             (no_spof) and
             (stripe_len >= 1)):
            return Pool.RAID_TYPE_RAID6
        # RAID 10
        elif ((data_redundancy == 2) and
             (pack_redundancy == 1) and
             (no_spof) and
             (stripe_len >= 1)):
            return Pool.RAID_TYPE_RAID10
        # Base on these data, we cannot determine RAID 15 or 51 and etc.
        # In stead of providing incorrect info, we choose to provide nothing.
        return Pool.RAID_TYPE_UNKNOWN

    @handle_cim_errors
    def pool_delete(self, pool, flags=0):
        """
        Delete a Pool via CIM_StorageConfigurationService.DeleteStoragePool
        """
        if not self.fallback_mode and \
           self._profile_is_supported(SNIA.BLK_SRVS_PROFILE,
                                      SNIA.SMIS_SPEC_VER_1_4,
                                      strict=False) is None:
            raise LsmError(ErrorNumber.NO_SUPPORT,
                           "SMI-S %s version %s is not supported" %
                           (SNIA.BLK_SRVS_PROFILE,
                            SNIA.SMIS_SPEC_VER_1_4))

        cim_pool = self._get_cim_instance_by_id('Pool', pool.id)
        cim_scs = self._get_class_instance(
            'CIM_StorageConfigurationService',
            'SystemName', pool.system_id)

        in_params = {'Pool': cim_pool.path}

        return self._pi("pool_delete", Smis.JOB_RETRIEVE_NONE,
                        *(self._c.InvokeMethod('DeleteStoragePool',
                                               cim_scs.path,
                                               **in_params)))[0]

    @handle_cim_errors
    def pool_create(self, system, pool_name, size_bytes,
                    raid_type=Pool.RAID_TYPE_UNKNOWN,
                    member_type=Pool.MEMBER_TYPE_UNKNOWN, flags=0):
        """
        Creating pool via
        CIM_StorageConfigurationService.CreateOrModifyStoragePool()
        from SMI-S 1.4+ "Block Services" profile.
        TODO: Each vendor are needing different parameters for
              CreateOrModifyStoragePool()
        """
        if not self.fallback_mode and \
           self._profile_is_supported(SNIA.BLK_SRVS_PROFILE,
                                      SNIA.SMIS_SPEC_VER_1_4,
                                      strict=False) is None:
            raise LsmError(ErrorNumber.NO_SUPPORT,
                           "SMI-S %s version %s is not supported" %
                           (SNIA.BLK_SRVS_PROFILE,
                            SNIA.SMIS_SPEC_VER_1_4))

        cim_sys = self._get_cim_instance_by_id('System', system.id)

        # we does not support defining thinp_type yet.
        # just using whatever provider set.

        in_params = {}
        if pool_name:
            in_params['ElementName'] = pool_name

        in_cim_exts_path = []
        if Pool.member_type_is_disk(member_type):
            disk_type = Pool.member_type_to_disk_type(member_type)
            if disk_type != Disk.DISK_TYPE_UNKNOWN:
                # We have to define InExtents for certain disk type.
                # SNIA 1.6.1 CIM_StorageSetting has these experimetal
                # properties:
                #       DiskType, InterconnectType, InterconnectSpeed,
                #       FormFactor, RPM, PortType.
                # But currently, no vendor implement that.
                # And there is no effective way to detect the free disks,
                # walking though all CIM_CompositeExtent is not a good idea.
                raise LsmError(ErrorNumber.NO_SUPPORT,
                               "The pool_create of SMI-S plugin does not "
                               "support defining disk type in member_type")
            else:
                # We depend on SMI-S provider to chose the disks for us.
                pass

        elif member_type == Pool.MEMBER_TYPE_POOL:
            # I(Gris) have lost my access to IBM DS8000 which support pool
            # over pool. I will raise NO_SUPPORT until got array to test on.
            raise LsmError(ErrorNumber.NO_SUPPORT,
                           "The pool_create of SMI-S plugin does not "
                           "support creating pool over pool(sub-pool) yet")

        elif member_type == Pool.MEMBER_TYPE_UNKNOWN:
            pass
        else:
            raise LsmError(ErrorNumber.INVALID_ARGUMENT,
                           "Got invalid member_type %d" % member_type)

        in_params['Size'] = pywbem.Uint64(size_bytes)

        if raid_type != Pool.RAID_TYPE_UNKNOWN:
            in_params['Goal'] = self._cim_st_path_for_goal(
                raid_type, cim_sys.path)

        cim_scs = self._get_class_instance(
            'CIM_StorageConfigurationService',
            'SystemName', system.id)

        in_params = self._pool_chg_paras_check(in_params, cim_sys.path)
        return self._pi("pool_create", Smis.JOB_RETRIEVE_POOL,
                        *(self._c.InvokeMethod(
                            'CreateOrModifyStoragePool',
                            cim_scs.path, **in_params)))

    @handle_cim_errors
    def _find_preset_cim_st(self, cim_cap_path, raid_type):
        """
        Usage:
            Find first proper CIM_StorageSetting under speficied
            CIM_StorageCapabilities by giving raid_type.
            Thin pool prefered.
        Parameter:
            cim_cap_path    # CIMInstanceName of CIM_StorageCapabilities
            raid_type       # Pool.RAID_TYPE_XXX
        Returns:
            cim_st          # CIMInstance of CIM_StorageSetting
                or
            None            # No match found
        """
        cim_sts = self._c.Associators(
            cim_cap_path,
            AssocClass='CIM_StorageSettingsAssociatedToCapabilities',
            ResultClass='CIM_StorageSetting',
            PropertyList=['ElementName',
                          'ThinProvisionedPoolType'])
        if not cim_sts:
            return None
        possible_element_names = []
        if raid_type == Pool.RAID_TYPE_JBOD:
            possible_element_names = ['JBOD']
        elif (raid_type == Pool.RAID_TYPE_RAID0 or
              raid_type == Pool.RAID_TYPE_NOT_APPLICABLE):
            possible_element_names = ['RAID0']
        elif raid_type == Pool.RAID_TYPE_RAID1:
            possible_element_names = ['RAID1']
        elif raid_type == Pool.RAID_TYPE_RAID3:
            possible_element_names = ['RAID3']
        elif raid_type == Pool.RAID_TYPE_RAID4:
            possible_element_names = ['RAID4']
        elif raid_type == Pool.RAID_TYPE_RAID5:
            possible_element_names = ['RAID5']
        elif raid_type == Pool.RAID_TYPE_RAID6:
            # According to SNIA suggest, RAID6 can also be writen as RAID5DP
            # and etc.
            possible_element_names = ['RAID6', 'RAID5DP']
        elif raid_type == Pool.RAID_TYPE_RAID10:
            possible_element_names = ['RAID10', 'RAID1+0']
        elif raid_type == Pool.RAID_TYPE_RAID50:
            possible_element_names = ['RAID50', 'RAID5+0']
        elif raid_type == Pool.RAID_TYPE_RAID60:
            possible_element_names = ['RAID60', 'RAID6+0', 'RAID5DP+0']
        elif raid_type == Pool.RAID_TYPE_RAID51:
            possible_element_names = ['RAID51', 'RAID5+1']
        elif raid_type == Pool.RAID_TYPE_RAID61:
            possible_element_names = ['RAID61', 'RAID6+1', 'RAID5DP+1']
        else:
            raise LsmError(ErrorNumber.INVALID_ARGUMENT,
                           "Got unknown RAID type: %d" % raid_type)

        chose_cim_sts = []
        for cim_st in cim_sts:
            if cim_st['ElementName'] in possible_element_names:
                chose_cim_sts.extend([cim_st])

        if len(chose_cim_sts) == 1:
            return chose_cim_sts[0]

        elif len(chose_cim_sts) > 1:
            # Perfer the thin pool. This is for EMC VNX which support both
            # think pool(less feature) and thin pool.
            for cim_st in chose_cim_sts:
                if cim_st['ThinProvisionedPoolType'] == \
                   Smis.DMTF_THINP_POOL_TYPE_ALLOCATED:
                    return cim_st

            # Return the first one if no thin pool setting found.
            return chose_cim_sts[0]

        return None

    def _cim_st_path_for_goal(self, raid_type, cim_sys_path):
        """
        Usage:
            Find out the array pre-defined CIM_StorageSetting for certain RAID
            Level. Check CIM_StorageSetting['ElementName'] for RAID type.
            Even SNIA defined a way to create new setting, but we find out
            that not a good way to follow.
            Pool.RAID_TYPE_NOT_APPLICABLE will be treat as RAID 0.
            # TODO: currently no check we will get one member for
            #       Pool.RAID_TYPE_NOT_APPLICABLE. Maybe we should replace
            #       this RAID type by RAID_0.
        Parameter:
            raid_type       # Tier.RAID_TYPE_XXX
            cim_sys_path    # CIMInstanceName of CIM_ComputerSystem.
        Returns:
            cim_st_path     # Found or created CIMInstanceName of
                            # CIM_StorageSetting
        Exceptions:
            LsmError
                ErrorNumber.NO_SUPPORT         # Failed to find out
                                               # suitable CIM_StorageSetting
        """
        chose_cim_st = None
        # We will try to find the existing CIM_StorageSetting
        # with ElementName equal to raid_type_str
        # potted(pre-defined) CIM_StorageSetting
        cim_pool_path = None
        cim_pools = self._c.Associators(cim_sys_path,
                                        ResultClass='CIM_StoragePool',
                                        PropertyList=['Primordial'])
        # Base on SNIA commanded, each array should provide a
        # Primordial pool.
        for cim_tmp_pool in cim_pools:
            if cim_tmp_pool['Primordial']:
                cim_pool_path = cim_tmp_pool.path
                break
        if not cim_pool_path:
            raise LsmError(ErrorNumber.NO_SUPPORT,
                           "Target storage array does not have any "
                           "Primordial CIM_StoragePool")
        cim_caps = self._c.Associators(
            cim_pool_path,
            ResultClass='CIM_StorageCapabilities',
            PropertyList=['ElementType'])
        for cim_cap in cim_caps:
            tmp_cim_st_set = self._find_preset_cim_st(cim_cap.path, raid_type)
            if tmp_cim_st_set:
                return tmp_cim_st_set.path
        raise LsmError(ErrorNumber.NO_SUPPORT,
                       "Current array does not support RAID type: %d"
                       % raid_type)

    def _pool_chg_paras_check(self, in_params, cim_sys_path):
        """
        Usage:
            CIM_StorageConfigurationCapabilities
            ['SupportedStoragePoolFeatures'] provide indication what
            parameters current array support when CreateOrModifyStoragePool()
            We will filter out the unsupported parameters.
        Parameter:
            in_params   # a dict will be used for CreateOrModifyStoragePool()
        Returns:
            new_in_params   # a dict of updated parameters
        """
        # EMC vendor specific value for thick pool.
        EMC_THINP_POOL_TYPE_THICK = 0
        new_in_params = in_params
        cim_scss = self._c.AssociatorNames(
            cim_sys_path,
            AssocClass='CIM_HostedService',
            ResultClass='CIM_StorageConfigurationService',)
        if len(cim_scss) != 1:
            return new_in_params
        cim_sccs = self._c.Associators(
            cim_scss[0],
            AssocClass='CIM_ElementCapabilities',
            ResultClass='CIM_StorageConfigurationCapabilities',
            PropertyList=['SupportedStoragePoolFeatures'])
        if len(cim_sccs) != 1:
            return new_in_params

        cur_features = cim_sccs[0]['SupportedStoragePoolFeatures']
        if 'InExtents' in new_in_params:
            if Smis.DMTF_ST_POOL_FEATURE_INEXTS not in cur_features:
                raise LsmError(ErrorNumber.NO_SUPPORT,
                               "Current array does not support " +
                               "creating Pool from Volume or Disk")
        if 'InPools' in new_in_params:
            if Smis.DMTF_ST_POOL_FEATURE_MULTI_INPOOL not in cur_features \
               and Smis.DMTF_ST_POOL_FEATURE_SINGLE_INPOOL not in cur_features:
                raise LsmError(ErrorNumber.NO_SUPPORT,
                               "Current array does not support " +
                               "creating Pool from Pool")
            if Smis.DMTF_ST_POOL_FEATURE_SINGLE_INPOOL in cur_features \
               and len(new_in_params['InPools']) > 1:
                raise LsmError(ErrorNumber.NO_SUPPORT,
                               "Current array does not support " +
                               "creating Pool from multiple pools")
        # Vendor specific check
        if cim_sys_path.classname == 'Clar_StorageSystem':
            if 'Goal' in new_in_params and 'ElementName' in new_in_params:
            ## EMC VNX/CX RAID Group should not define a ElementName.
                cim_st_path = new_in_params['Goal']
                cim_st = self._c.GetInstance(
                    cim_st_path,
                    PropertyList=['ThinProvisionedPoolType'],
                    LocalOnly=False)
                if cim_st['ThinProvisionedPoolType'] == \
                   EMC_THINP_POOL_TYPE_THICK:
                    del new_in_params['ElementName']
            if 'Pool' in new_in_params and 'Goal' in new_in_params:
            ## Expanding VNX/CX Pool/RAID Group shoud not define Goal
            ## Should we raise a error here?
                raise LsmError(ErrorNumber.NO_SUPPORT,
                               "EMC VNX/CX does not allowed change RAID " +
                               "type or add different RAID type tier")
        return new_in_params

    def _profile_is_supported(self, profile_name, spec_ver, strict=False,
                              raise_error=False):
        """
        Usage:
            Check whether we support certain profile at certain SNIA
            specification version.
            When strict == False(default), profile spec version later or equal
            than  require spec_ver will also be consider as found.
            When strict == True, only defined spec_version is allowed.
            Require self.cim_rps containing all CIM_RegisteredProfile
            Will raise LsmError(ErrorNumber.NO_SUPPORT, 'xxx') if raise_error
            is True when nothing found.
        Parameter:
            profile_name    # SNIA.XXXX_PROFILE
            spec_ver        # SNIA.SMIS_SPEC_VER_XXX
            strict          # False or True. If True, only defined
                            # spec_version is consider as supported
                            # If false, will return the maximum version of
                            # spec.
            raise_error     # Raise LsmError if not found
        Returns:
            None            # Not supported.
                or
            spec_int        # Integer. Converted by _spec_ver_str_to_num()
        """
        req_ver = _spec_ver_str_to_num(spec_ver)

        max_spec_ver_str = None
        max_spec_ver = None
        for cim_rp in self.cim_rps:
            if 'RegisteredName' not in cim_rp or \
               'RegisteredVersion' not in cim_rp:
                continue
            if cim_rp['RegisteredName'] == profile_name:
                # check spec version
                cur_ver = _spec_ver_str_to_num(cim_rp['RegisteredVersion'])

                if strict and cur_ver == req_ver:
                    return cur_ver
                elif cur_ver >= req_ver:
                    if max_spec_ver is None or \
                       cur_ver > max_spec_ver:
                        max_spec_ver = cur_ver
                        max_spec_ver_str = cim_rp['RegisteredVersion']
        if (strict or max_spec_ver is None) and raise_error:
            raise LsmError(ErrorNumber.NO_SUPPORT,
                           "SNIA SMI-S %s '%s' profile is not supported" %
                           (spec_ver, profile_name))

        return max_spec_ver

    def _root_cim_syss(self, property_list=None):
        """
        For fallback mode, this just enumerate CIM_ComputerSystem.
        We require vendor to implement profile registration when using
        "Multiple System Profile".
        For normal mode, this just find out the root CIM_ComputerSystem
        via:

                CIM_RegisteredProfile       # Root Profile('Array') in interop
                      |
                      | CIM_ElementConformsToProfile
                      v
                CIM_ComputerSystem          # vendor namespace

        We also assume no matter which version of root profile can lead to
        the same CIM_ComputerSystem instance.
        As CIM_ComputerSystem has no property indicate SNIA SMI-S version,
        this is assumption should work. Tested on EMC SMI-S provider which
        provide 1.4, 1.5, 1.6 root profile.
        """
        cim_scss_path = []
        id_pros = self._property_list_of_id('System', property_list)
        if property_list is None:
            property_list = id_pros
        else:
            property_list = _merge_list(property_list, id_pros)

        cim_syss = []
        if self.fallback_mode:
        # Fallback mode:
        # Find out the root CIM_ComputerSystem using the fallback method:
        #       CIM_StorageConfigurationService     # Enumerate
        #               |
        #               |   CIM_HostedService
        #               v
        #       CIM_ComputerSystem
        # If CIM_StorageConfigurationService is not support neither,
        # we enumerate CIM_ComputerSystem.
            try:
                cim_scss_path = self._c.EnumerateInstanceNames(
                    'CIM_StorageConfigurationService')
            except CIMError as e:
                # If array does not support CIM_StorageConfigurationService
                # we use CIM_ComputerSystem which is mandatory.
                # We might get some non-storage array listed as system.
                # but we would like to take that risk instead of
                # skipping basic support of old SMIS provider.
                if e[0] == pywbem.CIM_ERR_INVALID_CLASS:
                        cim_syss = self._c.EnumerateInstances(
                            'CIM_ComputerSystem',
                            PropertyList=property_list,
                            LocalOnly=False)
                else:
                    raise

            if not cim_syss:
                for cim_scs_path in cim_scss_path:
                    cim_tmp = None
                    # CIM_ComputerSystem is one-one map to
                    # CIM_StorageConfigurationService
                    cim_tmp = self._c.Associators(
                        cim_scs_path,
                        AssocClass='CIM_HostedService',
                        ResultClass='CIM_ComputerSystem',
                        PropertyList=property_list)
                    if cim_tmp and cim_tmp[0]:
                        cim_syss.extend([cim_tmp[0]])
        else:
            for cim_rp in self.cim_rps:
                if cim_rp['RegisteredName'] == SNIA.BLK_ROOT_PROFILE and\
                   cim_rp['RegisteredOrganization'] == SNIA.REG_ORG_CODE:
                    cim_syss = self._c.Associators(
                        cim_rp.path,
                        ResultClass='CIM_ComputerSystem',
                        AssocClass='CIM_ElementConformsToProfile',
                        PropertyList=property_list)
                    # Any version of 'Array' profile can get us to root
                    # CIM_ComputerSystem. startup() already has checked the
                    # 1.4 version
                    break
            if len(cim_syss) == 0:
                raise LsmError(ErrorNumber.NO_SUPPORT,
                               "Current SMI-S provider does not provide "
                               "the root CIM_ComputerSystem associated "
                               "to 'Array' CIM_RegisteredProfile. Try "
                               "add 'force_fallback_mode=yes' into URI")

        # System URI Filtering
        if self.system_list:
            needed_cim_syss = []
            for cim_sys in cim_syss:
                if self._sys_id(cim_sys) in self.system_list:
                    needed_cim_syss.extend([cim_sys])
            return needed_cim_syss
        else:
            return cim_syss

    @staticmethod
    def _cim_fc_tgt_to_lsm(cim_fc_tgt):
        """
        When provider support "Multiple Computer System" profile,
        CIM_FCPort['SystemName'] might not the name of root CIM_ComputerSystem

        Caller should update cim_fc_tgt['SystemName'] with the name of
        root CIM_ComputerSystem
        """
        port_id = md5(cim_fc_tgt['DeviceID'])
        port_type = _lsm_tgt_port_type_of_cim_fc_tgt(cim_fc_tgt)
        # SNIA define WWPN string as upper, no spliter, 16 digits.
        # No need to check.
        wwpn = _hex_string_format(cim_fc_tgt['PermanentAddress'], 16, 2)
        port_name = cim_fc_tgt['ElementName']
        system_id = cim_fc_tgt['SystemName']
        plugin_data = None
        return TargetPort(port_id, port_type, wwpn, wwpn, wwpn, port_name,
                          system_id, plugin_data)

    def _iscsi_node_name_of(self, cim_iscsi_pg_path):
        """
            CIM_iSCSIProtocolEndpoint
                    |
                    |
                    v
            CIM_SAPAvailableForElement
                    |
                    |
                    v
            CIM_SCSIProtocolController  # iSCSI Node

        """
        cim_spcs = self._c.Associators(
            cim_iscsi_pg_path,
            ResultClass='CIM_SCSIProtocolController',
            AssocClass='CIM_SAPAvailableForElement',
            PropertyList=['Name', 'NameFormat'])
        cim_iscsi_nodes = []
        for cim_spc in cim_spcs:
            if cim_spc.classname == 'Clar_MappingSCSIProtocolController':
                # EMC has vendor specific class which contain identical
                # properties of SPC for iSCSI node.
                continue
            if cim_spc['NameFormat'] == DMTF.SPC_NAME_FORMAT_ISCSI:
                cim_iscsi_nodes.extend([cim_spc])

        if len(cim_iscsi_nodes) == 0:
            raise LsmError(ErrorNumber.LSM_BUG,
                           "_iscsi_node_of(): No iSCSI node "
                           "CIM_SCSIProtocolController associated to %s"
                           % cim_iscsi_pg_path)
        if len(cim_iscsi_nodes) > 1:
            raise LsmError(ErrorNumber.LSM_BUG,
                           "_iscsi_node_of(): Got two or more iSCSI node "
                           "CIM_SCSIProtocolController associated to %s: %s"
                           % (cim_iscsi_pg_path, cim_iscsi_nodes))
        return cim_iscsi_nodes[0]['Name']

    def _cim_iscsi_pg_to_lsm(self, cim_iscsi_pg, system_id):
        """
        Return a list of TargetPort CIM_iSCSIProtocolEndpoint
        Associations:
            CIM_SCSIProtocolController  # iSCSI Node
                    ^
                    |   CIM_SAPAvailableForElement
                    |
            CIM_iSCSIProtocolEndpoint   # iSCSI Portal Group
                    |
                    |   CIM_BindsTo
                    v
            CIM_TCPProtocolEndpoint     # Need TCP port, default is 3260
                    |
                    |   CIM_BindsTo
                    v
            CIM_IPProtocolEndpoint      # Need IPv4 and IPv6 address
                    |
                    |   CIM_DeviceSAPImplementation
                    v
            CIM_EthernetPort            # Need MAC address (Optional)
        Assuming there is storage array support iSER
        (iSCSI over RDMA of Infinity Band),
        this method is only for iSCSI over TCP.
        """
        rc = []
        port_type = TargetPort.PORT_TYPE_ISCSI
        plugin_data = None
        cim_tcps = self._c.Associators(
            cim_iscsi_pg.path,
            ResultClass='CIM_TCPProtocolEndpoint',
            AssocClass='CIM_BindsTo',
            PropertyList=['PortNumber'])
        if len(cim_tcps) == 0:
            raise LsmError(ErrorNumber.LSM_BUG,
                           "_cim_iscsi_pg_to_lsm():  "
                           "No CIM_TCPProtocolEndpoint associated to %s"
                           % cim_iscsi_pg.path)
        iscsi_node_name = self._iscsi_node_name_of(cim_iscsi_pg.path)

        for cim_tcp in cim_tcps:
            tcp_port = cim_tcp['PortNumber']
            cim_ips = self._c.Associators(
                cim_tcp.path,
                ResultClass='CIM_IPProtocolEndpoint',
                AssocClass='CIM_BindsTo',
                PropertyList=['IPv4Address', 'IPv6Address', 'SystemName',
                              'EMCPortNumber', 'IPv6AddressType'])
            for cim_ip in cim_ips:
                ipv4_addr = ''
                ipv6_addr = ''
                # 'IPv4Address', 'IPv6Address' are optional in SMI-S 1.4.
                if 'IPv4Address' in cim_ip and cim_ip['IPv4Address']:
                    ipv4_addr = cim_ip['IPv4Address']
                if 'IPv6Address' in cim_ip and cim_ip['IPv6Address']:
                    ipv6_addr = cim_ip['IPv6Address']
                # 'IPv6AddressType' is not listed in SMI-S but in DMTF CIM
                # Schema
                # Only allow IPv6 Global Unicast Address, 6to4, and Unique
                # Local Address.
                if 'IPv6AddressType' in cim_ip and cim_ip['IPv6AddressType']:
                    ipv6_addr_type = cim_ip['IPv6AddressType']
                    if ipv6_addr_type != DMTF.IPV6_ADDR_TYPE_GUA and \
                       ipv6_addr_type != DMTF.IPV6_ADDR_TYPE_6TO4 and \
                       ipv6_addr_type != DMTF.IPV6_ADDR_TYPE_ULA:
                        ipv6_addr = ''

                # NetApp is using this kind of IPv6 address
                # 0000:0000:0000:0000:0000:0000:0a10:29d5
                # even when IPv6 is not enabled on their array.
                # It's not a legal IPv6 address anyway. No need to do
                # vendor check.
                if ipv6_addr[0:29] == '0000:0000:0000:0000:0000:0000':
                    ipv6_addr = ''

                if ipv4_addr is None and ipv6_addr is None:
                    continue
                cim_eths = self._c.Associators(
                    cim_ip.path,
                    ResultClass='CIM_EthernetPort',
                    AssocClass='CIM_DeviceSAPImplementation',
                    PropertyList=['PermanentAddress', 'ElementName'])
                nics = []
                # NetApp ONTAP cluster-mode show one IP bonded to multiple
                # ethernet,
                # Not suer it's their BUG or real ethernet channel bonding.
                # Waiting reply.
                if len(cim_eths) == 0:
                    nics = [('', '')]
                else:
                    for cim_eth in cim_eths:
                        mac_addr = ''
                        port_name = ''
                        if 'PermanentAddress' in cim_eth and \
                           cim_eth["PermanentAddress"]:
                            mac_addr = cim_eth["PermanentAddress"]
                        # 'ElementName' is optional in CIM_EthernetPort
                        if 'ElementName' in cim_eth and cim_eth["ElementName"]:
                            port_name = cim_eth['ElementName']
                        nics.extend([(mac_addr, port_name)])
                for nic in nics:
                    mac_address = nic[0]
                    port_name = nic[1]
                    if mac_address:
                        # Convert to lsm require form
                        mac_address = _hex_string_format(mac_address, 12, 2)

                    if ipv4_addr:
                        network_address = "%s:%s" % (ipv4_addr, tcp_port)
                        port_id = md5("%s:%s:%s" % (mac_address,
                                                    network_address,
                                                    iscsi_node_name))
                        rc.extend(
                            [TargetPort(port_id, port_type, iscsi_node_name,
                                        network_address, mac_address,
                                        port_name, system_id, plugin_data)])
                    if ipv6_addr:
                        # DMTF or SNIA did defined the IPv6 string format.
                        # we just guess here.
                        if len(ipv6_addr) == 39:
                            ipv6_addr = ipv6_addr.replace(':', '')
                            if len(ipv6_addr) == 32:
                                ipv6_addr = _hex_string_format(
                                    ipv6_addr, 32, 4)

                        network_address = "[%s]:%s" % (ipv6_addr, tcp_port)
                        port_id = md5("%s:%s:%s" % (mac_address,
                                                    network_address,
                                                    iscsi_node_name))
                        rc.extend(
                            [TargetPort(port_id, port_type, iscsi_node_name,
                                        network_address, mac_address,
                                        port_name, system_id, plugin_data)])
        return rc

    def _leaf_cim_syss_of(self, cim_sys_path, property_list=None):
        """
        Return a list of CIMInstance of CIM_ComputerSystem
        """
        if property_list is None:
            property_list = []

        max_loop_count = 10   # There is no storage array need 10 layer of
                              # Computer
        loop_counter = max_loop_count
        rc = []
        leaf_cim_syss = self._c.Associators(
            cim_sys_path,
            ResultClass='CIM_ComputerSystem',
            AssocClass='CIM_ComponentCS',
            Role='GroupComponent',
            ResultRole='PartComponent',
            PropertyList=property_list)
        if len(leaf_cim_syss) > 0:
            rc = leaf_cim_syss
            for cim_sys in leaf_cim_syss:
                rc.extend(self._leaf_cim_syss_of(cim_sys.path, property_list))

        return rc

    @handle_cim_errors
    def target_ports(self, search_key=None, search_value=None, flags=0):
        rc = []
        flag_fc_support = True      # we should try both for fallback mode
        flag_iscsi_support = True
        flag_multi_sys_support = False
        if not self.fallback_mode:
            flag_fc_support = self._profile_is_supported(
                SNIA.FC_TGT_PORT_PROFILE,
                SNIA.SMIS_SPEC_VER_1_4,
                strict=False,
                raise_error=False)
            # One more check for NetApp Typo:
            #   NetApp:     'FC Target Port'
            #   SMI-S:      'FC Target Ports'
            # Bug reported.
            if not flag_fc_support:
                flag_fc_support = self._profile_is_supported(
                    'FC Target Port',
                    SNIA.SMIS_SPEC_VER_1_4,
                    strict=False,
                    raise_error=False)

            flag_iscsi_support = self._profile_is_supported(
                SNIA.ISCSI_TGT_PORT_PROFILE,
                SNIA.SMIS_SPEC_VER_1_4,
                strict=False,
                raise_error=False)

            if flag_fc_support is None and flag_iscsi_support is None:
                raise LsmError(ErrorNumber.NO_SUPPORT,
                               "Target SMI-S provider does not support any of"
                               "these profiles: '%s %s', '%s %s'"
                               % (SNIA.SMIS_SPEC_VER_1_4,
                                  SNIA.FC_TGT_PORT_PROFILE,
                                  SNIA.SMIS_SPEC_VER_1_4,
                                  SNIA.ISCSI_TGT_PORT_PROFILE))
        if not self.fallback_mode:
            flag_multi_sys_support = self._profile_is_supported(
                SNIA.MULTI_SYS_PROFILE,
                SNIA.SMIS_SPEC_VER_1_4,
                strict=False,
                raise_error=False)

        cim_fc_tgt_pros = ['UsageRestriction', 'ElementName', 'SystemName',
                           'PermanentAddress', 'PortDiscriminator',
                           'LinkTechnology', 'DeviceID']

        if flag_fc_support:
            cim_fc_tgts = []
            if flag_multi_sys_support:
                # CIM_FCPort might be not belong to root cim_sys
                # In that case, CIM_FCPort['SystemName'] will not be
                # the name of root CIM_ComputerSystem
                cim_syss = self._root_cim_syss(
                    property_list=self._property_list_of_id('System'))
                for cim_sys in cim_syss:
                    cim_fc_tgts.extend(
                        self._c.Associators(
                            cim_sys.path,
                            AssocClass='CIM_SystemDevice',
                            ResultClass='CIM_FCPort',
                            PropertyList=cim_fc_tgt_pros))

                    system_id = self._sys_id(cim_sys)
                    leaf_cim_syss = self._leaf_cim_syss_of(cim_sys.path)
                    for leaf_cim_sys in leaf_cim_syss:
                        cur_cim_fc_tgts = self._c.Associators(
                            leaf_cim_sys.path,
                            AssocClass='CIM_SystemDevice',
                            ResultClass='CIM_FCPort',
                            PropertyList=cim_fc_tgt_pros)

                        # Update SystemName which will be used as system_id
                        for cim_fc_tgt in cur_cim_fc_tgts:
                            cim_fc_tgt['SystemName'] = system_id
                            cim_fc_tgts.extend([cim_fc_tgt])
            else:
                cim_fc_tgts = self._enumerate('CIM_FCPort',
                                              property_list=cim_fc_tgt_pros)
            for cim_fc_tgt in cim_fc_tgts:
                dmtf_usage = cim_fc_tgt['UsageRestriction']
                if dmtf_usage != DMTF.TGT_PORT_USAGE_FRONTEND_ONLY and \
                   dmtf_usage != DMTF.TGT_PORT_USAGE_UNRESTRICTED:
                    continue
                rc.extend([Smis._cim_fc_tgt_to_lsm(cim_fc_tgt)])

        if flag_iscsi_support:
            # As we need do more Associators() call in _cim_iscsi_pg_to_lsm()
            # We can not change SystemName as we did for FC/FCoE
            cim_iscsi_pg_pros = ['Role']

            cim_syss = self._root_cim_syss(
                property_list=self._property_list_of_id('System'))
            for cim_sys in cim_syss:
                cim_iscsi_pgs = self._c.Associators(
                    cim_sys.path,
                    AssocClass='CIM_HostedAccessPoint',
                    ResultClass='CIM_iSCSIProtocolEndpoint',
                    PropertyList=cim_iscsi_pg_pros)
                system_id = self._sys_id(cim_sys)
                if flag_multi_sys_support:
                    leaf_cim_syss = self._leaf_cim_syss_of(cim_sys.path)
                    for leaf_cim_sys in leaf_cim_syss:
                        cim_iscsi_pgs.extend(self._c.Associators(
                            leaf_cim_sys.path,
                            AssocClass='CIM_HostedAccessPoint',
                            ResultClass='CIM_iSCSIProtocolEndpoint',
                            PropertyList=cim_iscsi_pg_pros))
                for cim_iscsi_pg in cim_iscsi_pgs:
                    if cim_iscsi_pg['Role'] != DMTF.ISCSI_TGT_ROLE_TARGET:
                        continue
                    rc.extend(
                        self._cim_iscsi_pg_to_lsm(cim_iscsi_pg, system_id))
            # NetApp is sharing CIM_TCPProtocolEndpoint which
            # cause duplicate TargetPort. It's a long story, they heard my
            # bug report.
            if len(cim_syss) >= 1 and \
               cim_syss[0].classname == 'ONTAP_StorageSystem':
                id_list = []
                new_rc = []
                # We keep the original list order by not using dict.values()
                for lsm_tp in rc:
                    if lsm_tp.id not in id_list:
                        id_list.extend([lsm_tp.id])
                        new_rc.extend([lsm_tp])
                rc = new_rc

        return search_property(rc, search_key, search_value)
