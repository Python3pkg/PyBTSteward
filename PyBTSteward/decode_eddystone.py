#!/usr/bin/env python3

import logging
import os
import re
import signal
import struct
import subprocess
import sys
import time
from collections import namedtuple
import PyBTSteward.wpl_cfg_parser
import PyBTSteward.wpl_log
import PyBTSteward.wpl_stats
import bluetooth._bluetooth as bluez
import uuid
from . import __version__
from pprint import pprint
from PyBTSteward.wpl_cfg_parser import wpl_cfg
logger = logging.getLogger(__name__)

def decode_eddystone(state, config, ad_struct):
    logger.setLevel(config['Logging']['decode_eddy_loglevel'])

    """Ad structure decoder for Eddystone
  Returns a dictionary with the following fields if the ad structure is a
  valid mfg spec Eddystone structure:
    adstruct_bytes: <int> Number of bytes this ad structure consumed
    type: <string> 'eddystone' for Eddystone
  If it is an Eddystone UID ad structure, the dictionary also contains:
    sub_type: <string> 'uid'
    namespace: <string> hex string representing 10 byte namespace
    instance: <string> hex string representing 6 byte instance
    rssi_ref: <int> Reference signal @ 1m in dBm
  If it is an Eddystone URL ad structure, the dictionary also contains:
    sub_type: <string> 'url'
    url: <string> URL
    rssi_ref: <int> Reference signal @ 1m in dBm
  If it is an Eddystone TLM ad structure, the dictionary also contains:
    sub_type: <string> 'tlm'
    tlm_version: <int> Only version 0 is decoded to produce the next fields
    vbatt: <float> battery voltage in V
    temp: <float> temperature in degrees Celsius
    adv_cnt: <int> running count of advertisement frames
    sec_cnt: <float> time in seconds since boot
  If this isn't a valid Eddystone structure, it returns a dict with these
  fields:
    adstruct_bytes: <int> Number of bytes this ad structure consumed
    type: None for unknown
    """
    # Get the length of the ad structure (including the length byte)
    try:
        length = int(ad_struct[0]) + 1
        _collectedAs = 'int'
    except ValueError:
        logger.warn('failed back to collecting length from ord')
        length = ord(ad_struct[0]) + 1
        _collectedAs = 'str'
    #adstruct_bytes = ord(ad_struct[0]) + 1
    logger.debug('Length from byte[0]: %s (%s)', length, _collectedAs)
    logger.debug('Length of ad_struct: %s', len(ad_struct))
    adstruct_bytes = length
    # Create the return object
    ret = {'adstruct_bytes': adstruct_bytes, 'type': None}
    # Is our data long enough to decode as Eddystone?

    EddystoneCommon = namedtuple('EddystoneCommon', 'adstruct_bytes sd_length '+
                                 'sd_flags_type sd_flags_data uuid_list_len uuid_dt_val eddystone_uuid '+
                                 'eddy_len sd_type eddy_uuid_2 sub_type')
    if adstruct_bytes >= 5 and adstruct_bytes <= len(ad_struct):
        logger.debug('prepping EddystoneCommon tuple')
        # Decode the common part of the Eddystone data
        try:
            ec = EddystoneCommon._make(struct.unpack('<BBBBBBHBBHB', ad_struct[0:13]))
        except TypeError:
            #if we passed this as a bytestring, handle differently
            logger.warn('repacking packet for depaction into tuple: {}'.format(ad_struct[0:13]))
            ec = EddystoneCommon._make(struct.pack('<BBBBBBHBBHB', \
            [ ad_struct[0], ad_struct[1], ad_struct[2], ad_struct[3], \
            ad_struct[4], ad_struct[5], ad_struct[6:7], ad_struct[8], \
            ad_struct[9], ad_struct[10:11], ad_struct[12]]))

#        logger.debug('{}'.format(ec))
#        logger.debug('          uuid: {:02X}'.format(ec.eddystone_uuid))
#        logger.debug('adstruct_bytes: {:02X}'.format(ec.adstruct_bytes))
#        logger.debug('     sd_length: {:02X}'.format(ec.sd_length))
#        logger.debug(' sd_flags_type: {:02X}'.format(ec.sd_flags_type))
#        logger.debug(' sd_flags_data: {:02X}'.format(ec.sd_flags_data))
#        logger.debug(' uuid_list_len: {:02X}'.format(ec.uuid_list_len))
#        logger.debug('   uuid_dt_val: {:02X}'.format(ec.uuid_dt_val))
#        logger.debug('      eddy_len: {:02X}'.format(ec.eddy_len))
#        logger.debug('       sd_type: {:02X}'.format(ec.sd_type))
#        logger.debug('         uuid2: {:02X}'.format(ec.eddy_uuid_2))
#        logger.debug('      sub_type: {:02X}'.format(ec.sub_type))
        # Is this a valid Eddystone ad structure?

        if ec.eddystone_uuid == 0xFEAA and ec.sd_type == 0x16:
            # Fill in the return data we know at this point
            ret['type'] = 'eddystone'
            # Now select based on the sub type
            # Is this a UID sub type? (Accomodate beacons that either include or
            # exclude the reserved bytes)

            if ec.sub_type == 0x00 and (ec.eddy_len == 0x15 or
                                        ec.eddy_len == 0x17):
                ret['sub_type'] = 'uid'
                # Decode Eddystone UID data (without reserved bytes)
                EddystoneUID = namedtuple('EddystoneUID', 'rssi_ref namespace instance')
                ei = EddystoneUID._make(struct.unpack('>b10s6s', ad_struct[13:30]))
                # Fill in the return structure with the data we extracted
                logger.debug('EddyStone UID: {}'.format(ei))
                try:
                    ret['namespace'] = ''.join('{:02X}'.format(i) for i in ei.namespace)
                except TypeError:
                    logger.debug('interpolating Eddystone UID namespace from string')
                    ret['namespace'] = ''.join('%02X' % ord(c) for c in ei.namespace)
                try:
                    ret['instance'] = ''.join('{:02X}'.format(i) for i in ei.instance)
                except TypeError:
                    logger.debug('interpolating Eddystone UID instance from string')
                    ret['instance'] = ''.join('%02X' % ord(c) for c in ei.instance)
                ret['rssi_ref'] = ei.rssi_ref
# I think there's something to the last packet here, but not gonna fuck with it now.
#                if ec.eddy_len == 0x17:
#                    ret['rssi_fudge'] = str(ad_struct[len(ad_struct)])

            # Is this a URL sub type?
            if ec.sub_type == 0x10:
                ret['sub_type'] = 'url'
                # Decode Eddystone URL header
                EddyStoneURL = namedtuple('EddystoneURL', 'rssi_ref url_scheme')
                eu = EddyStoneURL._make(struct.unpack('>bB', ad_struct[13:20]))
                # Fill in the return structure with extracted data and init the URL
                ret['rssi_ref'] = eu.rssi_ref #- 41
                ret['rssi_fudge'] = int(ad_struct[len(ad_struct)])

                ret['url'] = ['http://www.', 'https://www.', 'http://', 'https://'] \
                      [eu.url_scheme & 0x03]
                # Go through the remaining bytes to build the URL
                for c in ad_struct[7:adstruct_bytes]:
                    # Get the character code
                    c_code = ord(c)
                    # Is this an expansion code?
                    if c_code < 14:
                        # Add the expansion code
                        ret['url'] += ['.com', '.org', '.edu', '.net', '.info', '.biz',
                                       '.gov'][c_code if c_code < 7 else c_code - 7]
                        # Add the slash if that variant is selected
                        if c_code < 7: ret['url'] += '/'
                    # Is this a graphic printable ASCII character?
                    if c_code > 0x20 and c_code < 0x7F:
                        # Add it to the URL
                        ret['url'] += c
            # Is this a TLM sub type?
            if ec.sub_type == 0x20 and ec.eddy_len == 0x11:
                ret['sub_type'] = 'tlm'
                # Decode Eddystone telemetry data
                EddystoneTLM = namedtuple('EddystoneTLM', 'tlm_version vbatt temp adv_cnt sec_cnt')
                #'EddystoneTLM','tlm_version','vbatt', 'temp', 'adv_cnt', 'sec_cnt')
                et = EddystoneTLM._make(struct.unpack('>BHhLL', ad_struct[13:26]))
                # Fill in generic TLM data
                ret['tlm_version'] = et.tlm_version
                # Fill the return structure with data if version 0
                if et.tlm_version == 0x00:
                    ret['vbatt'] = et.vbatt / 1000.00
                    ret['temp'] = et.temp / 256.00
                    ret['adv_cnt'] = et.adv_cnt
                    ret['sec_cnt'] = et.sec_cnt / 10.0
                logger.debug('EddyStone TLM: {}'.format(et))
    # Return the object
    return ret
