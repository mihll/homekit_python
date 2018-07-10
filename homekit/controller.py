#
# Copyright 2018 Joachim Lusiardi
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
import base64
import binascii

import uuid
import json
from distutils.util import strtobool
from json.decoder import JSONDecodeError
import time

from homekit.http_impl import HomeKitHTTPConnection, HttpContentTypes
from homekit.zeroconf_impl import discover_homekit_devices, find_device_ip_and_port
from homekit.protocol.statuscodes import HapStatusCodes
from homekit.exceptions import AccessoryNotFoundException, ConfigLoadingException, UnknownError, UnpairedException, \
    AuthenticationError, ConfigSavingException, AlreadyPairedException, FormatException
from homekit.http_impl.secure_http import SecureHttp
from homekit.protocol import get_session_keys, perform_pair_setup
from homekit.protocol.tlv import TLV, TlvParseException
from homekit.model.characteristics import CharacteristicsTypes, CharacteristicFormats


class Controller(object):
    """
    This class represents a HomeKit controller (normally your iPhone or iPad).
    """

    def __init__(self):
        """
        Initialize an empty controller. Use 'load_data()' to load the pairing data.
        """
        self.pairings = {}

    @staticmethod
    def discover(max_seconds=10):
        """
        Perform a Bonjour discovery for HomeKit accessory. The discovery will last for the given amount of seconds. The
        result will be a list of dicts. The keys of the dicts are:
         * name: the Bonjour name of the HomeKit accessory (i.e. Testsensor1._hap._tcp.local.)
         * address: the IP address of the accessory
         * port: the used port
         * c#: the configuration number
         * ff / flags: the numerical and human readable version of the feature flags (supports pairing or not, see table
                       5-8 page 69)
         * id: the accessory's pairing id
         * md: the model name of the accessory
         * pv: the protocol version
         * s#: the current state number
         * sf: the status flag (see table 5-9 page 70)
         * ci/category: the category identifier in numerical and human readable form. For more information see table
                        12-3 page 254 or homekit.Categories

        :param max_seconds: how long should the Bonjour service browser do the discovery (default 10s). See sleep for
                            more details
        :return: a list of dicts as described above
        """
        return discover_homekit_devices(max_seconds)

    @staticmethod
    def identify(accessory_id):
        """
        This call can be used to trigger the identification of a not yet paired accessory. A successful call should
        cause the accessory to perform some specific action by which it can be distinguished from the others (blink a
        LED for example).

        It uses the /identify url as described on page 88 of the spec.

        :param accessory_id: the accessory's pairing id (e.g. retrieved via discover)
        :raises AccessoryNotFoundException: if the accessory could not be looked up via Bonjour
        :raises AlreadyPairedException: if the accessory is already paired
        """
        connection_data = find_device_ip_and_port(accessory_id)
        if connection_data is None:
            raise AccessoryNotFoundException('Cannot find accessory with id "{i}".'.format(i=accessory_id))

        conn = HomeKitHTTPConnection(connection_data['ip'], port=connection_data['port'])
        conn.request('POST', '/identify')
        resp = conn.getresponse()

        # spec says status code 400 on any error (page 88). It also says status should be -70401 (which is "Request
        # denied due to insufficient privileges." table 5-12 page 80) but this sounds odd.
        if resp.code == 400:
            data = json.loads(resp.read().decode())
            code = data['status']
            raise AlreadyPairedException(
                'identify failed because: {reason} ({code}).'.format(reason=HapStatusCodes[code],
                                                                     code=code))
        conn.close()

    def get_pairings(self):
        """
        Returns a dict containing all pairings known to the controller.

        :return: the dict maps the aliases to Pairing objects
        """
        return self.pairings

    def load_data(self, filename):
        """
        Loads the pairing data of the controller from a file.

        :param filename: the file name of the pairing data
        :raises ConfigLoadingException: if the config could not be loaded. The reason is given in the message.
        """
        try:
            with open(filename, 'r') as input_fp:
                data = json.load(input_fp)
                for pairing_id in data:
                    self.pairings[pairing_id] = Pairing(data[pairing_id])
        except PermissionError as e:
            raise ConfigLoadingException('Could not open "{f}" due to missing permissions'.format(f=filename))
        except JSONDecodeError as e:
            raise ConfigLoadingException('Cannot parse "{f}" as JSON file'.format(f=filename))
        except FileNotFoundError as e:
            raise ConfigLoadingException('Could not open "{f}" because it does not exist'.format(f=filename))

    def save_data(self, filename):
        """
        Saves the pairing data of the controller to a file.

        :param filename: the file name of the pairing data
        :raises ConfigSavingException: if the config could not be saved. The reason is given in the message.
        """
        data = {}
        for pairing_id in self.pairings:
            # package visibility like in java would be nice here
            data[pairing_id] = self.pairings[pairing_id]._get_pairing_data()
        try:
            with open(filename, 'w') as output_fp:
                json.dump(data, output_fp, indent='  ')
        except PermissionError as e:
            raise ConfigSavingException('Could not write "{f}" due to missing permissions'.format(f=filename))
        except FileNotFoundError as e:
            raise ConfigSavingException(
                'Could not write "{f}" because it (or the folder) does not exist'.format(f=filename))

    def perform_pairing(self, alias, accessory_id, pin):
        """
        This performs a pairing attempt with the accessory identified by its id.

        Accessories can be found via the discover method. The id field is the accessory's for the second parameter.

        The required pin is either printed on the accessory or displayed. Must be a string of the form 'XXX-YY-ZZZ'.

        Important: no automatic saving of the pairing data is performed. If you don't do this, the information is lost
            and you have to reset the accessory!

        :param alias: the alias for the accessory in the controllers data
        :param accessory_id: the accessory's id
        :param pin: the accessory's pin
        :raises AccessoryNotFoundException: if no accessory with the given id can be found
        :raises AlreadyPairedException: if the alias was already used
        :raises UnavailableError: if the device is already paired
        :raises MaxTriesError: if the device received more than 100 unsuccessful attempts
        :raises BusyError: if a parallel pairing is ongoing
        :raises AuthenticationError: if the verification of the device's SRP proof fails
        :raises MaxPeersError: if the device cannot accept an additional pairing
        :raises IllegalData: if the verification of the accessory's data fails
        """
        if alias in self.pairings:
            raise AlreadyPairedException('Alias "{a}" is already paired.'.format(a=alias))
        connection_data = find_device_ip_and_port(accessory_id)
        if connection_data is None:
            raise AccessoryNotFoundException('Cannot find accessory with id "{i}".'.format(i=accessory_id))
        conn = HomeKitHTTPConnection(connection_data['ip'], port=connection_data['port'])
        pairing = perform_pair_setup(conn, pin, str(uuid.uuid4()))
        pairing['AccessoryIP'] = connection_data['ip']
        pairing['AccessoryPort'] = connection_data['port']
        self.pairings[alias] = Pairing(pairing)

    def remove_pairing(self, alias):
        """
        Remove a pairing between the controller and the accessory. The pairing data is delete on both ends, on the
        accessory and the controller.

        Important: no automatic saving of the pairing data is performed. If you don't do this, the accessory seems still
            to be paired on the next start of the application.

        :param alias: the controller's alias for the accessory
        :raises AuthenticationError: if the controller isn't authenticated to the accessory.
        :raises AccessoryNotFoundException: if the device can not be found via zeroconf
        :raises UnknownError: on unknown errors
        """
        # package visibility like in java would be nice here
        pairing_data = self.pairings[alias]._get_pairing_data()
        request_tlv = TLV.encode_dict({
            TLV.kTLVType_State: TLV.M1,
            TLV.kTLVType_Method: TLV.RemovePairing,
            TLV.kTLVType_Identifier: pairing_data['iOSPairingId'].encode()
        }).decode()  # decode is required because post needs a string representation
        session = Session(pairing_data)
        response = session.post('/pairings', request_tlv)
        data = response.read()
        data = TLV.decode_bytes(data)
        # handle the result, spec says, if it has only one entry with state == M2 we unpaired, else its an error.
        if len(data) == 1 and data[TLV.kTLVType_State] == TLV.M2:
            del self.pairings[alias]
        else:
            if data[TLV.kTLVType_Error] == TLV.kTLVError_Authentication:
                raise AuthenticationError('Remove pairing failed: missing authentication')
            else:
                raise UnknownError('Remove pairing failed: unknown error')


class Pairing(object):
    """
    This represents a paired HomeKit accessory.
    """

    def __init__(self, pairing_data):
        """
        Initialize a Pairing by using the data either loaded from file or obtained after calling
        Controller.perform_pairing().

        :param pairing_data:
        """
        self.pairing_data = pairing_data
        self.session = None

    def _get_pairing_data(self):
        """
        This method returns the internal pairing data. DO NOT mess around with it.

        :return: a dict containing the data
        """
        return self.pairing_data

    def get_accessories(self):
        """
        This retrieves a current set of accessories and characteristics behind this pairing.

        :return: the accessory data as described in the spec on page 73 and following
        """
        if not self.session:
            self.session = Session(self.pairing_data)
        response = self.session.get('/accessories')
        accessories = json.loads(response.read().decode())['accessories']
        self.pairing_data['accessories'] = accessories
        return accessories

    def list_pairings(self):
        """
        This method returns all pairings of a HomeKit accessory. This always includes the local controller and can only
        be done by an admin controller.

        The keys in the resulting dicts are:
         * pairingId: the pairing id of the controller
         * publicKey: the ED25519 long-term public key of the controller
         * permissions: bit value for the permissions
         * controllerType: either admin or regular

        :return: a list of dicts
        """
        if not self.session:
            self.session = Session(self.pairing_data)
        request_tlv = TLV.encode_dict({
            TLV.kTLVType_State: TLV.M1,
            TLV.kTLVType_Method: TLV.ListPairings
        })
        response = self.session.sec_http.post('/pairings', request_tlv.decode())
        data = response.read()
        data = TLV.decode_bytes_to_list(data)

        if not (data[0][0] == TLV.kTLVType_State and data[0][1] == TLV.M2):
            raise UnknownError('unexpected data received: ' + str(data))
        elif data[1][0] == TLV.kTLVType_Error and data[1][1] == TLV.kTLVError_Authentication:
            raise UnpairedException('Must be paired')
        else:
            tmp = []
            r = {}
            for d in data[1:]:
                if d[0] == TLV.kTLVType_Identifier:
                    r = {}
                    tmp.append(r)
                    r['pairingId'] = d[1].decode()
                if d[0] == TLV.kTLVType_PublicKey:
                    r['publicKey'] = d[1].hex()
                if d[0] == TLV.kTLVType_Permissions:
                    controller_type = 'regular'
                    if d[1] == b'\x01':
                        controller_type = 'admin'
                    r['permissions'] = int.from_bytes(d[1], byteorder='little')
                    r['controllerType'] = controller_type
            return tmp

    def get_characteristics(self, characteristics, include_meta=False, include_perms=False, include_type=False,
                            include_events=False):
        """
        This method is used to get the current readouts of any characteristic of the accessory.

        :param characteristics: a list of 2-tupels of accessory id and instance id
        :param include_meta: if True, include meta information about the characteristics. This contains the format and
                             the various constraints like maxLen and so on.
        :param include_perms: if True, include the permissions for the requested characteristics.
        :param include_type: if True, include the type of the characteristics in the result. See CharacteristicsTypes
                             for translations.
        :param include_events: if True on a characteristics that supports events, the result will contain information if
                               the controller currently is receiving events for that characteristic. Key is 'ev'.
        :return: a dict mapping 2-tupels of aid and iid to dicts with value or status and description, e.g.
                 {(1, 8): {'value': 23.42}
                  (1, 37): {'description': 'Resource does not exist.', 'status': -70409}
                 }
        """
        if not self.session:
            self.session = Session(self.pairing_data)
        url = '/characteristics?id=' + ','.join([str(x[0]) + '.' + str(x[1]) for x in characteristics])
        if include_meta:
            url += '&meta=1'
        else:
            url += '&meta=0'
        if include_perms:
            url += '&perms=1'
        else:
            url += '&perms=0'
        if include_type:
            url += '&type=1'
        else:
            url += '&type=0'
        if include_events:
            url += '&ev=1'
        else:
            url += '&ev=0'

        response = self.session.get(url)
        data = json.loads(response.read().decode())['characteristics']
        tmp = {}
        for c in data:
            key = (c['aid'], c['iid'])
            del c['aid']
            del c['iid']

            if 'status' in c and c['status'] == 0:
                del c['status']
            if 'status' in c and c['status'] != 0:
                c['description'] = HapStatusCodes[c['status']]
            tmp[key] = c
        return tmp

    def put_characteristics(self, characteristics, do_conversion=False):
        """
        Update the values of writable characteristics. The characteristics have to be identified by accessory id (aid),
        instance id (iid). If do_conversion is False (the default), the value must be of proper format for the
        characteristic since no conversion is done. If do_conversion is True, the value is converted.

        :param characteristics: a list of 3-tupels of accessory id, instance id and the value
        :param do_conversion: select if conversion is done (False is default)
        :return: a list of 3-tupels of accessory id, instance id and status for all characteristics that could not be
                 written successfully. The status values can be looked up in HapStatusCodes.
        :raises FormatException: if conversion is requested and the input value could not be converted to the
                target type
        """
        if not self.session:
            self.session = Session(self.pairing_data)
        if 'accessories' not in self.pairing_data:
            self.get_accessories()
        data = []
        characteristics_set = set()
        for characteristic in characteristics:
            aid = characteristic[0]
            iid = characteristic[1]
            value = characteristic[2]
            if do_conversion:
                # evaluate proper format
                c_format = None
                for d in self.pairing_data['accessories']:
                    if 'aid' in d and d['aid'] == aid:
                        for s in d['services']:
                            for c in s['characteristics']:
                                if 'iid' in c and c['iid'] == iid:
                                    c_format = c['format']
                # try to do conversion, if it fails report proper exception
                try:
                    value = check_convert_value(value, c_format)
                except FormatException as e:
                    message = 'Error occurred on conversion for {a}.{i}: {m}'.format(a=aid, i=iid, f=c_format, m=str(e))
                    raise FormatException(message)
            characteristics_set.add('{a}.{i}'.format(a=aid, i=iid))
            data.append({'aid': aid, 'iid': iid, 'value': value})
        data = json.dumps({'characteristics': data})
        response = self.session.put('/characteristics', data)
        if response.code != 204:
            data = response.read().decode()
            data = json.loads(data)['characteristics']
            data = {(d['aid'], d['iid']): {'status': d['status'], 'description': HapStatusCodes[d['status']]} for d in
                    data}
            return data
        return {}

    def get_events(self, characteristics, callback_fun, max_events=-1, max_seconds=-1):
        """
        This function is called to register for events on characteristics and receive them. Each time events are
        received a call back function is invoked. By that the caller gets information about the events.

        The characteristics are identified via their proper accessory id (aid) and instance id (iid).

        The call back function takes a list of 3-tupels of aid, iid and the value, e.g.:
          [(1, 9, 26.1), (1, 10, 30.5)]

        If the input contains characteristics without the event permission or any other error, the function will return
        a dict containing tupels of aid and iid for each requested characteristic with error. Those who would have
        worked are not in the result.

        :param characteristics: a list of 2-tupels of accessory id (aid) and instance id (iid)
        :param callback_fun: a function that is called each time events were recieved
        :param max_events: number of reported events, default value -1 means unlimited
        :param max_seconds: number of seconds to wait for events, default value -1 means unlimited
        :return: a dict mapping 2-tupels of aid and iid to dicts with status and description, e.g.
                 {(1, 37): {'description': 'Notification is not supported for characteristic.', 'status': -70406}}
        """
        if not self.session:
            self.session = Session(self.pairing_data)
        data = []
        characteristics_set = set()
        for characteristic in characteristics:
            aid = characteristic[0]
            iid = characteristic[1]
            characteristics_set.add('{a}.{i}'.format(a=aid, i=iid))
            data.append({'aid': aid, 'iid': iid, 'ev': True})
        data = json.dumps({'characteristics': data})
        response = self.session.put('/characteristics', data)

        # handle error responses
        if response.code != 204:
            tmp = {}
            data = json.loads(response.read().decode())
            print(data)
            for characteristic in data['characteristics']:
                status = characteristic['status']
                if status == 0:
                    continue
                aid = characteristic['aid']
                iid = characteristic['iid']
                tmp[(aid, iid)] = {'status': status, 'description': HapStatusCodes[status]}
            return tmp

        # wait for incoming events
        event_count = 0
        s = time.time()
        while (max_events == -1 or event_count < max_events) and (max_seconds == -1 or s + max_seconds >= time.time()):
            r = self.session.sec_http.handle_event_response()
            body = r.read().decode()
            if len(body) > 0:
                r = json.loads(body)
                tmp = []
                for c in r['characteristics']:
                    tmp.append((c['aid'], c['iid'], c['value']))
                callback_fun(tmp)
                event_count += 1
        return {}

    def identify(self):
        """
        This call can be used to trigger the identification of a paired accessory. A successful call should
        cause the accessory to perform some specific action by which it can be distinguished from the others (blink a
        LED for example).

        It uses the identify characteristic as described on page 152 of the spec.

        :return True, if the identification was run, False otherwise
        """
        if not self.session:
            self.session = Session(self.pairing_data)
        if 'accessories' not in self.pairing_data:
            self.get_accessories()

        # we are looking for a characteristic of the identify type
        identify_type = CharacteristicsTypes.get_uuid(CharacteristicsTypes.IDENTIFY)

        # search all accessories, all services and all characteristics
        for accessory in self.pairing_data['accessories']:
            aid = accessory['aid']
            for service in accessory['services']:
                for characteristic in service['characteristics']:
                    iid = characteristic['iid']
                    c_type = CharacteristicsTypes.get_uuid(characteristic['type'])
                    if identify_type == c_type:
                        # found the identify characteristic, so let's put a value there
                        self.put_characteristics([(aid, iid, True)])
                        return True
        return False


class Session(object):
    def __init__(self, pairing_data):
        """

        :param pairing_data:
        :raises AccessoryNotFoundException: if the device can not be found via zeroconf
        """
        connected = False
        if 'AccessoryIP' in pairing_data and 'AccessoryPort' in pairing_data:
            # if it is known, try it
            accessory_ip = pairing_data['AccessoryIP']
            accessory_port = pairing_data['AccessoryPort']
            conn = HomeKitHTTPConnection(accessory_ip, port=accessory_port)
            try:
                conn.connect()
                c2a_key, a2c_key = get_session_keys(conn, pairing_data)
                connected = True
            except Exception as e:
                connected = False
        if not connected:
            # no connection yet, so ip / port might have changed and we need to fall back to slow zeroconf lookup
            device_id = pairing_data['AccessoryPairingID']
            connection_data = find_device_ip_and_port(device_id)
            if connection_data is None:
                raise AccessoryNotFoundException(
                    'Device {id} not found'.format(id=pairing_data['AccessoryPairingID']))
            conn = HomeKitHTTPConnection(connection_data['ip'], port=connection_data['port'])
            pairing_data['AccessoryIP'] = connection_data['ip']
            pairing_data['AccessoryPort'] = connection_data['port']
            c2a_key, a2c_key = get_session_keys(conn, pairing_data)

        self.sock = conn.sock
        self.c2a_key = c2a_key
        self.a2c_key = a2c_key
        self.pairing_data = pairing_data
        self.sec_http = SecureHttp(self)

    def get_from_pairing_data(self, key):
        if key not in self.pairing_data:
            return None
        return self.pairing_data[key]

    def set_in_pairing_data(self, key, value):
        self.pairing_data[key] = value

    def get(self, url):
        """
        Perform HTTP get via the encrypted session.
        :param url: The url to request
        :return: a homekit.http_impl.HttpResponse object
        """
        return self.sec_http.get(url)

    def put(self, url, body, content_type=HttpContentTypes.JSON):
        """
        Perform HTTP put via the encrypted session.
        :param url: The url to request
        :param body: the body of the put request
        :param content_type: the content of the content-type header
        :return: a homekit.http_impl.HttpResponse object
        """
        return self.sec_http.put(url, body, content_type)

    def post(self, url, body, content_type=HttpContentTypes.JSON):
        """
        Perform HTTP post via the encrypted session.
        :param url: The url to request
        :param body: the body of the post request
        :param content_type: the content of the content-type header
        :return: a homekit.http_impl.HttpResponse object
        """
        return self.sec_http.post(url, body, content_type)


def check_convert_value(val, target_type):
    """
    Checks if the given value is of the given type or is convertible into the type. If the value is not convertible, a
    HomeKitTypeException is thrown.

    :param val: the original value
    :param target_type: the target type of the conversion
    :return: the converted value
    :raises FormatException: if the input value could not be converted to the target type
    """
    if target_type == CharacteristicFormats.bool:
        try:
            val = strtobool(str(val))
        except ValueError:
            raise FormatException('"{v}" is no valid "{t}"!'.format(v=val, t=target_type))
    if target_type in [CharacteristicFormats.uint64, CharacteristicFormats.uint32,
                       CharacteristicFormats.uint16, CharacteristicFormats.uint8,
                       CharacteristicFormats.int]:
        try:
            val = int(val)
        except ValueError:
            raise FormatException('"{v}" is no valid "{t}"!'.format(v=val, t=target_type))
    if target_type == CharacteristicFormats.float:
        try:
            val = float(val)
        except ValueError:
            raise FormatException('"{v}" is no valid "{t}"!'.format(v=val, t=target_type))
    if target_type == CharacteristicFormats.data:
        try:
            base64.decodebytes(val.encode())
        except binascii.Error:
            raise FormatException('"{v}" is no valid "{t}"!'.format(v=val, t=target_type))
    if target_type == CharacteristicFormats.tlv8:
        try:
            tmp_bytes = base64.decodebytes(val.encode())
            TLV.decode_bytes(tmp_bytes)
        except (binascii.Error, TlvParseException):
            raise FormatException('"{v}" is no valid "{t}"!'.format(v=val, t=target_type))
    return val
