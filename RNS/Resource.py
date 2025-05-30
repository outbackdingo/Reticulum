# Reticulum License
#
# Copyright (c) 2016-2025 Mark Qvist
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# - The Software shall not be used in any kind of system which includes amongst
#   its functions the ability to purposefully do harm to human beings.
#
# - The Software shall not be used, directly or indirectly, in the creation of
#   an artificial intelligence, machine learning or language model training
#   dataset, including but not limited to any use that contributes to the
#   training or development of such a model or algorithm.
#
# - The above copyright notice and this permission notice shall be included in
#   all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import RNS
import os
import bz2
import math
import time
import struct
import tempfile
import threading
from threading import Lock
from .vendor import umsgpack as umsgpack
from time import sleep

class Resource:
    """
    The Resource class allows transferring arbitrary amounts
    of data over a link. It will automatically handle sequencing,
    compression, coordination and checksumming.

    :param data: The data to be transferred. Can be *bytes* or an open *file handle*. See the :ref:`Filetransfer Example<example-filetransfer>` for details.
    :param link: The :ref:`RNS.Link<api-link>` instance on which to transfer the data.
    :param advertise: Optional. Whether to automatically advertise the resource. Can be *True* or *False*.
    :param auto_compress: Optional. Whether to auto-compress the resource. Can be *True* or *False*.
    :param callback: An optional *callable* with the signature *callback(resource)*. Will be called when the resource transfer concludes.
    :param progress_callback: An optional *callable* with the signature *callback(resource)*. Will be called whenever the resource transfer progress is updated.
    """

    # The initial window size at beginning of transfer
    WINDOW               = 4

    # Absolute minimum window size during transfer
    WINDOW_MIN           = 2

    # The maximum window size for transfers on slow links
    WINDOW_MAX_SLOW      = 10

    # The maximum window size for transfers on very slow links
    WINDOW_MAX_VERY_SLOW = 4

    # The maximum window size for transfers on fast links
    WINDOW_MAX_FAST      = 75
    
    # For calculating maps and guard segments, this
    # must be set to the global maximum window.
    WINDOW_MAX           = WINDOW_MAX_FAST
    
    # If the fast rate is sustained for this many request
    # rounds, the fast link window size will be allowed.
    FAST_RATE_THRESHOLD  = WINDOW_MAX_SLOW - WINDOW - 2

    # If the very slow rate is sustained for this many request
    # rounds, window will be capped to the very slow limit.
    VERY_SLOW_RATE_THRESHOLD = 2

    # If the RTT rate is higher than this value,
    # the max window size for fast links will be used.
    # The default is 50 Kbps (the value is stored in
    # bytes per second, hence the "/ 8").
    RATE_FAST            = (50*1000) / 8

    # If the RTT rate is lower than this value,
    # the window size will be capped at .
    # The default is 50 Kbps (the value is stored in
    # bytes per second, hence the "/ 8").
    RATE_VERY_SLOW       = (2*1000) / 8

    # The minimum allowed flexibility of the window size.
    # The difference between window_max and window_min
    # will never be smaller than this value.
    WINDOW_FLEXIBILITY   = 4

    # Number of bytes in a map hash
    MAPHASH_LEN          = 4
    SDU                  = RNS.Packet.MDU
    RANDOM_HASH_SIZE     = 4

    # This is an indication of what the
    # maximum size a resource should be, if
    # it is to be handled within reasonable
    # time constraint, even on small systems.
    #
    # A small system in this regard is
    # defined as a Raspberry Pi, which should
    # be able to compress, encrypt and hash-map
    # the resource in about 10 seconds.
    #
    # This constant will be used when determining
    # how to sequence the sending of large resources.
    #
    # Capped at 16777215 (0xFFFFFF) per segment to
    # fit in 3 bytes in resource advertisements.
    MAX_EFFICIENT_SIZE      = 1 * 1024 * 1024 - 1
    RESPONSE_MAX_GRACE_TIME = 10

    # Max metadata size is 16777215 (0xFFFFFF) bytes
    METADATA_MAX_SIZE       = 16 * 1024 * 1024 - 1
    
    # The maximum size to auto-compress with
    # bz2 before sending.
    AUTO_COMPRESS_MAX_SIZE = MAX_EFFICIENT_SIZE

    PART_TIMEOUT_FACTOR           = 4
    PART_TIMEOUT_FACTOR_AFTER_RTT = 2
    PROOF_TIMEOUT_FACTOR          = 3
    MAX_RETRIES                   = 16
    MAX_ADV_RETRIES               = 4
    SENDER_GRACE_TIME             = 10.0
    PROCESSING_GRACE              = 1.0
    RETRY_GRACE_TIME              = 0.25
    PER_RETRY_DELAY               = 0.5

    WATCHDOG_MAX_SLEEP            = 1

    HASHMAP_IS_NOT_EXHAUSTED = 0x00
    HASHMAP_IS_EXHAUSTED = 0xFF

    # Status constants
    NONE            = 0x00
    QUEUED          = 0x01
    ADVERTISED      = 0x02
    TRANSFERRING    = 0x03
    AWAITING_PROOF  = 0x04
    ASSEMBLING      = 0x05
    COMPLETE        = 0x06
    FAILED          = 0x07
    CORRUPT         = 0x08
    REJECTED        = 0x00

    @staticmethod
    def reject(advertisement_packet):
        try:
            adv = ResourceAdvertisement.unpack(advertisement_packet.plaintext)
            resource_hash = adv.h
            reject_packet = RNS.Packet(advertisement_packet.link, resource_hash, context=RNS.Packet.RESOURCE_RCL)
            reject_packet.send()

        except Exception as e:
            RNS.log(f"An error ocurred while rejecting advertised resource: {e}", RNS.LOG_ERROR)
            RNS.trace_exception(e)

    @staticmethod
    def accept(advertisement_packet, callback=None, progress_callback = None, request_id = None):
        try:
            adv = ResourceAdvertisement.unpack(advertisement_packet.plaintext)

            resource = Resource(None, advertisement_packet.link, request_id = request_id)
            resource.status = Resource.TRANSFERRING

            resource.flags                = adv.f
            resource.size                 = adv.t
            resource.total_size           = adv.d
            resource.uncompressed_size    = adv.d
            resource.hash                 = adv.h
            resource.original_hash        = adv.o
            resource.random_hash          = adv.r
            resource.hashmap_raw          = adv.m
            resource.encrypted            = True if resource.flags & 0x01 else False
            resource.compressed           = True if resource.flags >> 1 & 0x01 else False
            resource.initiator            = False
            resource.callback             = callback
            resource.__progress_callback  = progress_callback
            resource.total_parts          = int(math.ceil(resource.size/float(resource.sdu)))
            resource.received_count       = 0
            resource.outstanding_parts    = 0
            resource.parts                = [None] * resource.total_parts
            resource.window               = Resource.WINDOW
            resource.window_max           = Resource.WINDOW_MAX_SLOW
            resource.window_min           = Resource.WINDOW_MIN
            resource.window_flexibility   = Resource.WINDOW_FLEXIBILITY
            resource.last_activity        = time.time()
            resource.started_transferring = resource.last_activity

            resource.storagepath          = RNS.Reticulum.resourcepath+"/"+resource.original_hash.hex()
            resource.meta_storagepath     = resource.storagepath+".meta"
            resource.segment_index        = adv.i
            resource.total_segments       = adv.l
            
            if adv.l > 1: resource.split = True
            else: resource.split = False

            if adv.x: resource.has_metadata = True
            else:     resource.has_metadata = False

            resource.hashmap = [None] * resource.total_parts
            resource.hashmap_height = 0
            resource.waiting_for_hmu = False
            resource.receiving_part = False
            resource.consecutive_completed_height = -1

            previous_window = resource.link.get_last_resource_window()
            previous_eifr   = resource.link.get_last_resource_eifr()
            if previous_window:
                resource.window = previous_window
            if previous_eifr:
                resource.previous_eifr = previous_eifr
            
            if not resource.link.has_incoming_resource(resource):
                resource.link.register_incoming_resource(resource)

                RNS.log(f"Accepting resource advertisement for {RNS.prettyhexrep(resource.hash)}. Transfer size is {RNS.prettysize(resource.size)} in {resource.total_parts} parts.", RNS.LOG_DEBUG)
                if resource.link.callbacks.resource_started != None:
                    try:
                        resource.link.callbacks.resource_started(resource)
                    except Exception as e:
                        RNS.log("Error while executing resource started callback from "+str(resource)+". The contained exception was: "+str(e), RNS.LOG_ERROR)

                resource.hashmap_update(0, resource.hashmap_raw)
                resource.watchdog_job()
                return resource

            else:
                RNS.log("Ignoring resource advertisement for "+RNS.prettyhexrep(resource.hash)+", resource already transferring", RNS.LOG_DEBUG)
                return None

        except Exception as e:
            RNS.log("Could not decode resource advertisement, dropping resource", RNS.LOG_DEBUG)
            return None

    # Create a resource for transmission to a remote destination
    # The data passed can be either a bytes-array or a file opened
    # in binary read mode.
    def __init__(self, data, link, metadata=None, advertise=True, auto_compress=True, callback=None, progress_callback=None,
                 timeout = None, segment_index = 1, original_hash = None, request_id = None, is_response = False, sent_metadata_size=0):
        
        data_size = None
        resource_data = None
        self.assembly_lock = False
        self.preparing_next_segment = False
        self.next_segment = None
        self.metadata = None
        self.has_metadata = False
        self.metadata_size = sent_metadata_size

        if metadata != None:
            packed_metadata = umsgpack.packb(metadata)
            metadata_size   = len(packed_metadata)
            if metadata_size > Resource.METADATA_MAX_SIZE:
                raise SystemError("Resource metadata size exceeded")
            else:
                self.metadata = struct.pack(">I", metadata_size)[1:] + packed_metadata
                self.metadata_size = len(self.metadata)
                self.has_metadata = True
        else:
            self.metadata = b""

        if data != None:
            if not hasattr(data, "read") and self.metadata_size + len(data) > Resource.MAX_EFFICIENT_SIZE:
                original_data = data
                data_size = len(original_data)
                data = tempfile.TemporaryFile()
                data.write(original_data)
                del original_data

        if hasattr(data, "read"):
            if data_size == None: data_size = os.stat(data.name).st_size
            self.total_size = data_size + self.metadata_size

            if self.total_size <= Resource.MAX_EFFICIENT_SIZE:
                self.total_segments = 1
                self.segment_index  = 1
                self.split          = False
                resource_data = data.read()
                data.close()

            else:
                # self.total_segments = ((data_size-1)//Resource.MAX_EFFICIENT_SIZE)+1
                # self.segment_index  = segment_index
                # self.split          = True
                # seek_index          = segment_index-1
                # seek_position       = seek_index*Resource.MAX_EFFICIENT_SIZE

                self.total_segments = ((self.total_size-1)//Resource.MAX_EFFICIENT_SIZE)+1
                self.segment_index  = segment_index
                self.split          = True
                seek_index          = segment_index-1
                first_read_size     = Resource.MAX_EFFICIENT_SIZE - self.metadata_size

                if segment_index == 1:
                    seek_position     = 0
                    segment_read_size = first_read_size
                else:
                    seek_position     = first_read_size + ((seek_index-1)*Resource.MAX_EFFICIENT_SIZE)
                    segment_read_size = Resource.MAX_EFFICIENT_SIZE

                data.seek(seek_position)
                resource_data = data.read(segment_read_size)
                self.input_file = data

        elif isinstance(data, bytes):
            data_size = len(data)
            self.total_size = data_size + self.metadata_size
            
            resource_data = data
            self.total_segments = 1
            self.segment_index  = 1
            self.split          = False

        elif data == None:
            pass

        else:
            raise TypeError("Invalid data instance type passed to resource initialisation")

        if resource_data:
            if self.has_metadata: data = self.metadata + resource_data
            else:                 data = resource_data

        self.status = Resource.NONE
        self.link = link
        if self.link.mtu:
            self.sdu = self.link.mtu - RNS.Reticulum.HEADER_MAXSIZE - RNS.Reticulum.IFAC_MIN_SIZE
        else:
            self.sdu = link.mdu or Resource.SDU
        self.max_retries = Resource.MAX_RETRIES
        self.max_adv_retries = Resource.MAX_ADV_RETRIES
        self.retries_left = self.max_retries
        self.timeout_factor = self.link.traffic_timeout_factor
        self.part_timeout_factor = Resource.PART_TIMEOUT_FACTOR
        self.sender_grace_time = Resource.SENDER_GRACE_TIME
        self.hmu_retry_ok = False
        self.watchdog_lock = False
        self.__watchdog_job_id = 0
        self.__progress_callback = progress_callback
        self.rtt = None
        self.rtt_rxd_bytes = 0
        self.req_sent = 0
        self.req_resp_rtt_rate = 0
        self.rtt_rxd_bytes_at_part_req = 0
        self.req_data_rtt_rate = 0
        self.eifr = None
        self.previous_eifr = None
        self.fast_rate_rounds = 0
        self.very_slow_rate_rounds = 0
        self.request_id = request_id
        self.started_transferring = None
        self.is_response = is_response
        self.auto_compress = auto_compress

        self.req_hashlist = []
        self.receiver_min_consecutive_height = 0

        if timeout != None:
            self.timeout = timeout
        else:
            self.timeout = self.link.rtt * self.link.traffic_timeout_factor

        if data != None:
            self.initiator         = True
            self.callback          = callback
            self.uncompressed_data = data

            compression_began = time.time()
            if (auto_compress and len(self.uncompressed_data) <= Resource.AUTO_COMPRESS_MAX_SIZE):
                RNS.log("Compressing resource data...", RNS.LOG_EXTREME)
                self.compressed_data   = bz2.compress(self.uncompressed_data)
                RNS.log("Compression completed in "+str(round(time.time()-compression_began, 3))+" seconds", RNS.LOG_EXTREME)
            else:
                self.compressed_data   = self.uncompressed_data

            self.uncompressed_size = len(self.uncompressed_data)
            self.compressed_size   = len(self.compressed_data)

            if (self.compressed_size < self.uncompressed_size and auto_compress):
                saved_bytes = len(self.uncompressed_data) - len(self.compressed_data)
                RNS.log("Compression saved "+str(saved_bytes)+" bytes, sending compressed", RNS.LOG_EXTREME)

                self.data  = b""
                self.data += RNS.Identity.get_random_hash()[:Resource.RANDOM_HASH_SIZE]
                self.data += self.compressed_data
                
                self.compressed = True

            else:
                self.data  = b""
                self.data += RNS.Identity.get_random_hash()[:Resource.RANDOM_HASH_SIZE]
                self.data += self.uncompressed_data

                self.compressed = False
                self.compressed_data = None
                if auto_compress:
                    RNS.log("Compression did not decrease size, sending uncompressed", RNS.LOG_EXTREME)

            self.compressed_data = None
            self.uncompressed_data = None

            # Resources handle encryption directly to
            # make optimal use of packet MTU on an entire
            # encrypted stream. The Resource instance will
            # use it's underlying link directly to encrypt.
            self.data = self.link.encrypt(self.data)
            self.encrypted = True

            self.size = len(self.data)
            self.sent_parts = 0
            hashmap_entries = int(math.ceil(self.size/float(self.sdu)))
            self.total_parts = hashmap_entries
                
            hashmap_ok = False
            while not hashmap_ok:
                hashmap_computation_began = time.time()
                RNS.log("Starting resource hashmap computation with "+str(hashmap_entries)+" entries...", RNS.LOG_EXTREME)

                self.random_hash       = RNS.Identity.get_random_hash()[:Resource.RANDOM_HASH_SIZE]
                self.hash = RNS.Identity.full_hash(data+self.random_hash)
                self.truncated_hash = RNS.Identity.truncated_hash(data+self.random_hash)
                self.expected_proof = RNS.Identity.full_hash(data+self.hash)

                if original_hash == None:
                    self.original_hash = self.hash
                else:
                    self.original_hash = original_hash

                self.parts  = []
                self.hashmap = b""
                collision_guard_list = []
                for i in range(0,hashmap_entries):
                    data = self.data[i*self.sdu:(i+1)*self.sdu]
                    map_hash = self.get_map_hash(data)

                    if map_hash in collision_guard_list:
                        RNS.log("Found hash collision in resource map, remapping...", RNS.LOG_DEBUG)
                        hashmap_ok = False
                        break
                    else:
                        hashmap_ok = True
                        collision_guard_list.append(map_hash)
                        if len(collision_guard_list) > ResourceAdvertisement.COLLISION_GUARD_SIZE:
                            collision_guard_list.pop(0)

                        part = RNS.Packet(link, data, context=RNS.Packet.RESOURCE)
                        part.pack()
                        part.map_hash = map_hash

                        self.hashmap += part.map_hash
                        self.parts.append(part)

                RNS.log("Hashmap computation concluded in "+str(round(time.time()-hashmap_computation_began, 3))+" seconds", RNS.LOG_EXTREME)

            self.data = None
            if advertise:
                self.advertise()
        else:
            self.receive_lock = Lock()
            

    def hashmap_update_packet(self, plaintext):
        if not self.status == Resource.FAILED:
            self.last_activity = time.time()
            self.retries_left = self.max_retries

            update = umsgpack.unpackb(plaintext[RNS.Identity.HASHLENGTH//8:])
            self.hashmap_update(update[0], update[1])


    def hashmap_update(self, segment, hashmap):
        if not self.status == Resource.FAILED:
            self.status = Resource.TRANSFERRING
            seg_len = ResourceAdvertisement.HASHMAP_MAX_LEN
            hashes = len(hashmap)//Resource.MAPHASH_LEN
            for i in range(0,hashes):
                if self.hashmap[i+segment*seg_len] == None:
                    self.hashmap_height += 1
                self.hashmap[i+segment*seg_len] = hashmap[i*Resource.MAPHASH_LEN:(i+1)*Resource.MAPHASH_LEN]

            self.waiting_for_hmu = False
            self.request_next()

    def get_map_hash(self, data):
        return RNS.Identity.full_hash(data+self.random_hash)[:Resource.MAPHASH_LEN]

    def advertise(self):
        """
        Advertise the resource. If the other end of the link accepts
        the resource advertisement it will begin transferring.
        """
        thread = threading.Thread(target=self.__advertise_job, daemon=True)
        thread.start()

        if self.segment_index < self.total_segments:
            prepare_thread = threading.Thread(target=self.__prepare_next_segment, daemon=True)
            prepare_thread.start()

    def __advertise_job(self):
        self.advertisement_packet = RNS.Packet(self.link, ResourceAdvertisement(self).pack(), context=RNS.Packet.RESOURCE_ADV)
        while not self.link.ready_for_new_resource():
            self.status = Resource.QUEUED
            sleep(0.25)

        try:
            self.advertisement_packet.send()
            self.last_activity = time.time()
            self.started_transferring = self.last_activity
            self.adv_sent = self.last_activity
            self.rtt = None
            self.status = Resource.ADVERTISED
            self.retries_left = self.max_adv_retries
            self.link.register_outgoing_resource(self)
            RNS.log("Sent resource advertisement for "+RNS.prettyhexrep(self.hash), RNS.LOG_EXTREME)
        except Exception as e:
            RNS.log("Could not advertise resource, the contained exception was: "+str(e), RNS.LOG_ERROR)
            self.cancel()
            return

        self.watchdog_job()

    def update_eifr(self):
        if self.rtt == None:
            rtt = self.link.rtt
        else:
            rtt = self.rtt

        if self.req_data_rtt_rate != 0:
            expected_inflight_rate = self.req_data_rtt_rate*8
        else:
            if self.previous_eifr != None:
                expected_inflight_rate = self.previous_eifr
            else:
                expected_inflight_rate = self.link.establishment_cost*8 / rtt

        self.eifr = expected_inflight_rate
        if self.link: self.link.expected_rate = self.eifr

    def watchdog_job(self):
        thread = threading.Thread(target=self.__watchdog_job, daemon=True)
        thread.start()

    def __watchdog_job(self):
        self.__watchdog_job_id += 1
        this_job_id = self.__watchdog_job_id

        while self.status < Resource.ASSEMBLING and this_job_id == self.__watchdog_job_id:
            while self.watchdog_lock:
                sleep(0.025)

            sleep_time = None
            if self.status == Resource.ADVERTISED:
                sleep_time = (self.adv_sent+self.timeout+Resource.PROCESSING_GRACE)-time.time()
                if sleep_time < 0:
                    if self.retries_left <= 0:
                        RNS.log("Resource transfer timeout after sending advertisement", RNS.LOG_DEBUG)
                        self.cancel()
                        sleep_time = 0.001
                    else:
                        try:
                            RNS.log("No part requests received, retrying resource advertisement...", RNS.LOG_DEBUG)
                            self.retries_left -= 1
                            self.advertisement_packet = RNS.Packet(self.link, ResourceAdvertisement(self).pack(), context=RNS.Packet.RESOURCE_ADV)
                            self.advertisement_packet.send()
                            self.last_activity = time.time()
                            self.adv_sent = self.last_activity
                            sleep_time = 0.001
                        except Exception as e:
                            RNS.log("Could not resend advertisement packet, cancelling resource. The contained exception was: "+str(e), RNS.LOG_VERBOSE)
                            self.cancel()
                    

            elif self.status == Resource.TRANSFERRING:
                if not self.initiator:
                    retries_used = self.max_retries - self.retries_left
                    extra_wait = retries_used * Resource.PER_RETRY_DELAY

                    self.update_eifr()
                    expected_tof_remaining = (self.outstanding_parts*self.sdu*8)/self.eifr

                    if self.req_resp_rtt_rate != 0:
                        sleep_time = self.last_activity + self.part_timeout_factor*expected_tof_remaining + Resource.RETRY_GRACE_TIME + extra_wait - time.time()
                    else:
                        sleep_time = self.last_activity + self.part_timeout_factor*((3*self.sdu)/self.eifr) + Resource.RETRY_GRACE_TIME + extra_wait - time.time()
                    
                    # TODO: Remove debug at some point
                    # RNS.log(f"EIFR {RNS.prettyspeed(self.eifr)}, ETOF {RNS.prettyshorttime(expected_tof_remaining)} ", RNS.LOG_DEBUG, pt=True)
                    # RNS.log(f"Resource ST {RNS.prettyshorttime(sleep_time)}, RTT {RNS.prettyshorttime(self.rtt or self.link.rtt)}, {self.outstanding_parts} left", RNS.LOG_DEBUG, pt=True)
                    
                    if sleep_time < 0:
                        if self.retries_left > 0:
                            ms = "" if self.outstanding_parts == 1 else "s"
                            RNS.log("Timed out waiting for "+str(self.outstanding_parts)+" part"+ms+", requesting retry", RNS.LOG_DEBUG)
                            if self.window > self.window_min:
                                self.window -= 1
                                if self.window_max > self.window_min:
                                    self.window_max -= 1
                                    if (self.window_max - self.window) > (self.window_flexibility-1):
                                        self.window_max -= 1

                            sleep_time = 0.001
                            self.retries_left -= 1
                            self.waiting_for_hmu = False
                            self.request_next()
                        else:
                            self.cancel()
                            sleep_time = 0.001
                else:
                    max_extra_wait = sum([(r+1) * Resource.PER_RETRY_DELAY for r in range(self.MAX_RETRIES)])
                    max_wait = self.rtt * self.timeout_factor * self.max_retries + self.sender_grace_time + max_extra_wait
                    sleep_time = self.last_activity + max_wait - time.time()
                    if sleep_time < 0:
                        RNS.log("Resource timed out waiting for part requests", RNS.LOG_DEBUG)
                        self.cancel()
                        sleep_time = 0.001

            elif self.status == Resource.AWAITING_PROOF:
                # Decrease timeout factor since proof packets are
                # significantly smaller than full req/resp roundtrip
                self.timeout_factor = Resource.PROOF_TIMEOUT_FACTOR

                sleep_time = self.last_part_sent + (self.rtt*self.timeout_factor+self.sender_grace_time) - time.time()
                if sleep_time < 0:
                    if self.retries_left <= 0:
                        RNS.log("Resource timed out waiting for proof", RNS.LOG_DEBUG)
                        self.cancel()
                        sleep_time = 0.001
                    else:
                        RNS.log("All parts sent, but no resource proof received, querying network cache...", RNS.LOG_DEBUG)
                        self.retries_left -= 1
                        expected_data = self.hash + self.expected_proof
                        expected_proof_packet = RNS.Packet(self.link, expected_data, packet_type=RNS.Packet.PROOF, context=RNS.Packet.RESOURCE_PRF)
                        expected_proof_packet.pack()
                        RNS.Transport.cache_request(expected_proof_packet.packet_hash, self.link)
                        self.last_part_sent = time.time()
                        sleep_time = 0.001

            elif self.status == Resource.REJECTED:
                sleep_time = 0.001

            if sleep_time == 0:
                RNS.log("Warning! Link watchdog sleep time of 0!", RNS.LOG_DEBUG)
            if sleep_time == None or sleep_time < 0:
                RNS.log("Timing error, cancelling resource transfer.", RNS.LOG_ERROR)
                self.cancel()
            
            if sleep_time != None:
                sleep(min(sleep_time, Resource.WATCHDOG_MAX_SLEEP))

    def assemble(self):
        if not self.status == Resource.FAILED:
            try:
                self.status = Resource.ASSEMBLING
                stream = b"".join(self.parts)

                if self.encrypted: data = self.link.decrypt(stream)
                else: data = stream

                # Strip off random hash
                data = data[Resource.RANDOM_HASH_SIZE:]

                if self.compressed: self.data = bz2.decompress(data)
                else: self.data = data

                calculated_hash = RNS.Identity.full_hash(self.data+self.random_hash)
                if calculated_hash == self.hash:
                    if self.has_metadata and self.segment_index == 1:
                        # TODO: Add early metadata_ready callback
                        metadata_size = self.data[0] << 16 | self.data[1] << 8 | self.data[2]
                        packed_metadata = self.data[3:3+metadata_size]
                        metadata_file = open(self.meta_storagepath, "wb")
                        metadata_file.write(packed_metadata)
                        metadata_file.close()
                        del packed_metadata
                        data = self.data[3+metadata_size:]
                    else:
                        data = self.data

                    self.file = open(self.storagepath, "ab")
                    self.file.write(data)
                    self.file.close()
                    self.status = Resource.COMPLETE
                    del data
                    self.prove()
                
                else: self.status = Resource.CORRUPT


            except Exception as e:
                RNS.log("Error while assembling received resource.", RNS.LOG_ERROR)
                RNS.log("The contained exception was: "+str(e), RNS.LOG_ERROR)
                self.status = Resource.CORRUPT

            self.link.resource_concluded(self)

            if self.segment_index == self.total_segments:
                if self.callback != None:
                    if not os.path.isfile(self.meta_storagepath):
                        self.metadata = None
                    else:
                        metadata_file = open(self.meta_storagepath, "rb")
                        self.metadata = umsgpack.unpackb(metadata_file.read())
                        metadata_file.close()
                        try: os.unlink(self.meta_storagepath)
                        except Exception as e:
                            RNS.log(f"Error while cleaning up resource metadata file, the contained exception was: {e}", RNS.LOG_ERROR)

                    self.data = open(self.storagepath, "rb")
                    try: self.callback(self)
                    except Exception as e:
                        RNS.log("Error while executing resource assembled callback from "+str(self)+". The contained exception was: "+str(e), RNS.LOG_ERROR)

                try:
                    if hasattr(self.data, "close") and callable(self.data.close): self.data.close()
                    if os.path.isfile(self.storagepath): os.unlink(self.storagepath)

                except Exception as e:
                    RNS.log(f"Error while cleaning up resource files, the contained exception was: {e}", RNS.LOG_ERROR)
            else:
                RNS.log("Resource segment "+str(self.segment_index)+" of "+str(self.total_segments)+" received, waiting for next segment to be announced", RNS.LOG_DEBUG)


    def prove(self):
        if not self.status == Resource.FAILED:
            try:
                proof = RNS.Identity.full_hash(self.data+self.hash)
                proof_data = self.hash+proof
                proof_packet = RNS.Packet(self.link, proof_data, packet_type=RNS.Packet.PROOF, context=RNS.Packet.RESOURCE_PRF)
                proof_packet.send()
                RNS.Transport.cache(proof_packet, force_cache=True)
            except Exception as e:
                RNS.log("Could not send proof packet, cancelling resource", RNS.LOG_DEBUG)
                RNS.log("The contained exception was: "+str(e), RNS.LOG_DEBUG)
                self.cancel()

    def __prepare_next_segment(self):
        # Prepare the next segment for advertisement
        RNS.log(f"Preparing segment {self.segment_index+1} of {self.total_segments} for resource {self}", RNS.LOG_DEBUG)
        self.preparing_next_segment = True
        self.next_segment = Resource(
            self.input_file, self.link,
            callback = self.callback,
            segment_index = self.segment_index+1,
            original_hash=self.original_hash,
            progress_callback = self.__progress_callback,
            request_id = self.request_id,
            is_response = self.is_response,
            advertise = False,
            auto_compress = self.auto_compress,
            sent_metadata_size = self.metadata_size,
        )

    def validate_proof(self, proof_data):
        if not self.status == Resource.FAILED:
            if len(proof_data) == RNS.Identity.HASHLENGTH//8*2:
                if proof_data[RNS.Identity.HASHLENGTH//8:] == self.expected_proof:
                    self.status = Resource.COMPLETE
                    self.link.resource_concluded(self)
                    if self.segment_index == self.total_segments:
                        # If all segments were processed, we'll
                        # signal that the resource sending concluded
                        if self.callback != None:
                            try: self.callback(self)
                            except Exception as e: RNS.log("Error while executing resource concluded callback from "+str(self)+". The contained exception was: "+str(e), RNS.LOG_ERROR)
                            finally:
                                try:
                                    if hasattr(self, "input_file"):
                                        if hasattr(self.input_file, "close") and callable(self.input_file.close): self.input_file.close()
                                except Exception as e: RNS.log("Error while closing resource input file: "+str(e), RNS.LOG_ERROR)
                        else:
                            try:
                                if hasattr(self, "input_file"):
                                    if hasattr(self.input_file, "close") and callable(self.input_file.close): self.input_file.close()
                            except Exception as e: RNS.log("Error while closing resource input file: "+str(e), RNS.LOG_ERROR)
                    else:
                        # Otherwise we'll recursively create the
                        # next segment of the resource
                        if not self.preparing_next_segment:
                            RNS.log(f"Next segment preparation for resource {self} was not started yet, manually preparing now. This will cause transfer slowdown.", RNS.LOG_WARNING)
                            self.__prepare_next_segment()

                        while self.next_segment == None: time.sleep(0.05)

                        self.data = None
                        self.metadata = None
                        self.parts = None
                        self.input_file = None
                        self.link = None
                        self.req_hashlist = None
                        self.hashmap = None

                        self.next_segment.advertise()
                else:
                    pass
            else:
                pass


    def receive_part(self, packet):
        with self.receive_lock:

            self.receiving_part = True
            self.last_activity = time.time()
            self.retries_left = self.max_retries

            if self.req_resp == None:
                self.req_resp = self.last_activity
                rtt = self.req_resp-self.req_sent
                
                self.part_timeout_factor = Resource.PART_TIMEOUT_FACTOR_AFTER_RTT
                if self.rtt == None:
                    self.rtt = self.link.rtt
                    self.watchdog_job()
                elif rtt < self.rtt:
                    self.rtt = max(self.rtt - self.rtt*0.05, rtt)
                elif rtt > self.rtt:
                    self.rtt = min(self.rtt + self.rtt*0.05, rtt)

                if rtt > 0:
                    req_resp_cost = len(packet.raw)+self.req_sent_bytes
                    self.req_resp_rtt_rate = req_resp_cost / rtt

                    if self.req_resp_rtt_rate > Resource.RATE_FAST and self.fast_rate_rounds < Resource.FAST_RATE_THRESHOLD:
                        self.fast_rate_rounds += 1

                        if self.fast_rate_rounds == Resource.FAST_RATE_THRESHOLD:
                            self.window_max = Resource.WINDOW_MAX_FAST

            if not self.status == Resource.FAILED:
                self.status = Resource.TRANSFERRING
                part_data = packet.data
                part_hash = self.get_map_hash(part_data)

                consecutive_index = self.consecutive_completed_height if self.consecutive_completed_height >= 0 else 0
                i = consecutive_index
                for map_hash in self.hashmap[consecutive_index:consecutive_index+self.window]:
                    if map_hash == part_hash:
                        if self.parts[i] == None:

                            # Insert data into parts list
                            self.parts[i] = part_data
                            self.rtt_rxd_bytes += len(part_data)
                            self.received_count += 1
                            self.outstanding_parts -= 1

                            # Update consecutive completed pointer
                            if i == self.consecutive_completed_height + 1:
                                self.consecutive_completed_height = i
                            
                            cp = self.consecutive_completed_height + 1
                            while cp < len(self.parts) and self.parts[cp] != None:
                                self.consecutive_completed_height = cp
                                cp += 1

                            if self.__progress_callback != None:
                                try:
                                    self.__progress_callback(self)
                                except Exception as e:
                                    RNS.log("Error while executing progress callback from "+str(self)+". The contained exception was: "+str(e), RNS.LOG_ERROR)

                    i += 1

                self.receiving_part = False

                if self.received_count == self.total_parts and not self.assembly_lock:
                    self.assembly_lock = True
                    self.assemble()
                elif self.outstanding_parts == 0:
                    # TODO: Figure out if there is a mathematically
                    # optimal way to adjust windows
                    if self.window < self.window_max:
                        self.window += 1
                        if (self.window - self.window_min) > (self.window_flexibility-1):
                            self.window_min += 1

                    if self.req_sent != 0:
                        rtt = time.time()-self.req_sent
                        req_transferred = self.rtt_rxd_bytes - self.rtt_rxd_bytes_at_part_req

                        if rtt != 0:
                            self.req_data_rtt_rate = req_transferred/rtt
                            self.update_eifr()
                            self.rtt_rxd_bytes_at_part_req = self.rtt_rxd_bytes

                            if self.req_data_rtt_rate > Resource.RATE_FAST and self.fast_rate_rounds < Resource.FAST_RATE_THRESHOLD:
                                self.fast_rate_rounds += 1

                                if self.fast_rate_rounds == Resource.FAST_RATE_THRESHOLD:
                                    self.window_max = Resource.WINDOW_MAX_FAST

                            if self.fast_rate_rounds == 0 and self.req_data_rtt_rate < Resource.RATE_VERY_SLOW and self.very_slow_rate_rounds < Resource.VERY_SLOW_RATE_THRESHOLD:
                                self.very_slow_rate_rounds += 1

                                if self.very_slow_rate_rounds == Resource.VERY_SLOW_RATE_THRESHOLD:
                                    self.window_max = Resource.WINDOW_MAX_VERY_SLOW

                    self.request_next()
            else:
                self.receiving_part = False

    # Called on incoming resource to send a request for more data
    def request_next(self):
        while self.receiving_part:
            sleep(0.001)

        if not self.status == Resource.FAILED:
            if not self.waiting_for_hmu:
                self.outstanding_parts = 0
                hashmap_exhausted = Resource.HASHMAP_IS_NOT_EXHAUSTED
                requested_hashes = b""

                i = 0; pn = self.consecutive_completed_height+1
                search_start = pn
                search_size = self.window
                
                for part in self.parts[search_start:search_start+search_size]:
                    if part == None:
                        part_hash = self.hashmap[pn]
                        if part_hash != None:
                            requested_hashes += part_hash
                            self.outstanding_parts += 1
                            i += 1
                        else:
                            hashmap_exhausted = Resource.HASHMAP_IS_EXHAUSTED

                    pn += 1
                    if i >= self.window or hashmap_exhausted == Resource.HASHMAP_IS_EXHAUSTED:
                        break

                hmu_part = bytes([hashmap_exhausted])
                if hashmap_exhausted == Resource.HASHMAP_IS_EXHAUSTED:
                    last_map_hash = self.hashmap[self.hashmap_height-1]
                    hmu_part += last_map_hash
                    self.waiting_for_hmu = True

                request_data = hmu_part + self.hash + requested_hashes
                request_packet = RNS.Packet(self.link, request_data, context = RNS.Packet.RESOURCE_REQ)

                try:
                    request_packet.send()
                    self.last_activity = time.time()
                    self.req_sent = self.last_activity
                    self.req_sent_bytes = len(request_packet.raw)
                    self.req_resp = None

                except Exception as e:
                    RNS.log("Could not send resource request packet, cancelling resource", RNS.LOG_DEBUG)
                    RNS.log("The contained exception was: "+str(e), RNS.LOG_DEBUG)
                    self.cancel()

    # Called on outgoing resource to make it send more data
    def request(self, request_data):
        if not self.status == Resource.FAILED:
            rtt = time.time() - self.adv_sent
            if self.rtt == None:
                self.rtt = rtt

            if self.status != Resource.TRANSFERRING:
                self.status = Resource.TRANSFERRING
                self.watchdog_job()

            self.retries_left = self.max_retries

            wants_more_hashmap = True if request_data[0] == Resource.HASHMAP_IS_EXHAUSTED else False
            pad = 1+Resource.MAPHASH_LEN if wants_more_hashmap else 1

            requested_hashes = request_data[pad+RNS.Identity.HASHLENGTH//8:]

            # Define the search scope
            search_start = self.receiver_min_consecutive_height
            search_end   = self.receiver_min_consecutive_height+ResourceAdvertisement.COLLISION_GUARD_SIZE

            map_hashes = []
            for i in range(0,len(requested_hashes)//Resource.MAPHASH_LEN):
                map_hash = requested_hashes[i*Resource.MAPHASH_LEN:(i+1)*Resource.MAPHASH_LEN]
                map_hashes.append(map_hash)

            search_scope = self.parts[search_start:search_end]
            requested_parts = list(filter(lambda part: part.map_hash in map_hashes, search_scope))

            for part in requested_parts:
                try:
                    if not part.sent:
                        part.send()
                        self.sent_parts += 1
                    else:
                        part.resend()

                    self.last_activity = time.time()
                    self.last_part_sent = self.last_activity

                except Exception as e:
                    RNS.log("Resource could not send parts, cancelling transfer!", RNS.LOG_DEBUG)
                    RNS.log("The contained exception was: "+str(e), RNS.LOG_DEBUG)
                    self.cancel()
            
            if wants_more_hashmap:
                last_map_hash = request_data[1:Resource.MAPHASH_LEN+1]
                
                part_index   = self.receiver_min_consecutive_height
                search_start = part_index
                search_end   = self.receiver_min_consecutive_height+ResourceAdvertisement.COLLISION_GUARD_SIZE
                for part in self.parts[search_start:search_end]:
                    part_index += 1
                    if part.map_hash == last_map_hash:
                        break

                self.receiver_min_consecutive_height = max(part_index-1-Resource.WINDOW_MAX, 0)

                if part_index % ResourceAdvertisement.HASHMAP_MAX_LEN != 0:
                    RNS.log("Resource sequencing error, cancelling transfer!", RNS.LOG_ERROR)
                    self.cancel()
                    return
                else:
                    segment = part_index // ResourceAdvertisement.HASHMAP_MAX_LEN

                
                hashmap_start = segment*ResourceAdvertisement.HASHMAP_MAX_LEN
                hashmap_end   = min((segment+1)*ResourceAdvertisement.HASHMAP_MAX_LEN, len(self.parts))

                hashmap = b""
                for i in range(hashmap_start,hashmap_end):
                    hashmap += self.hashmap[i*Resource.MAPHASH_LEN:(i+1)*Resource.MAPHASH_LEN]

                hmu = self.hash+umsgpack.packb([segment, hashmap])
                hmu_packet = RNS.Packet(self.link, hmu, context = RNS.Packet.RESOURCE_HMU)

                try:
                    hmu_packet.send()
                    self.last_activity = time.time()
                except Exception as e:
                    RNS.log("Could not send resource HMU packet, cancelling resource", RNS.LOG_DEBUG)
                    RNS.log("The contained exception was: "+str(e), RNS.LOG_DEBUG)
                    self.cancel()

            if self.sent_parts == len(self.parts):
                self.status = Resource.AWAITING_PROOF
                self.retries_left = 3

            if self.__progress_callback != None:
                try:
                    self.__progress_callback(self)
                except Exception as e:
                    RNS.log("Error while executing progress callback from "+str(self)+". The contained exception was: "+str(e), RNS.LOG_ERROR)

    def cancel(self):
        """
        Cancels transferring the resource.
        """
        if self.status < Resource.COMPLETE:
            self.status = Resource.FAILED
            if self.initiator:
                if self.link.status == RNS.Link.ACTIVE:
                    try:
                        cancel_packet = RNS.Packet(self.link, self.hash, context=RNS.Packet.RESOURCE_ICL)
                        cancel_packet.send()
                    except Exception as e:
                        RNS.log("Could not send resource cancel packet, the contained exception was: "+str(e), RNS.LOG_ERROR)
                self.link.cancel_outgoing_resource(self)
            else:
                self.link.cancel_incoming_resource(self)
            
            if self.callback != None:
                try:
                    self.link.resource_concluded(self)
                    self.callback(self)
                except Exception as e:
                    RNS.log("Error while executing callbacks on resource cancel from "+str(self)+". The contained exception was: "+str(e), RNS.LOG_ERROR)

    def _rejected(self):
        if self.status < Resource.COMPLETE:
            if self.initiator:
                self.status = Resource.REJECTED
                self.link.cancel_outgoing_resource(self)
                if self.callback != None:
                    try:
                        self.link.resource_concluded(self)
                        self.callback(self)
                    except Exception as e:
                        RNS.log("Error while executing callbacks on resource reject from "+str(self)+". The contained exception was: "+str(e), RNS.LOG_ERROR)

    def set_callback(self, callback):
        self.callback = callback

    def progress_callback(self, callback):
        self.__progress_callback = callback

    def get_progress(self):
        """
        :returns: The current progress of the resource transfer as a *float* between 0.0 and 1.0.
        """
        if self.status == RNS.Resource.COMPLETE and self.segment_index == self.total_segments:
            return 1.0
        
        elif self.initiator:
            if not self.split:
                self.processed_parts = self.sent_parts
                self.progress_total_parts = float(self.total_parts)

            else:
                is_last_segment = self.segment_index != self.total_segments
                total_segments = self.total_segments
                processed_segments = self.segment_index-1

                current_segment_parts = self.total_parts
                max_parts_per_segment = math.ceil(Resource.MAX_EFFICIENT_SIZE/self.sdu)

                previously_processed_parts = processed_segments*max_parts_per_segment

                if current_segment_parts < max_parts_per_segment:
                    current_segment_factor = max_parts_per_segment / current_segment_parts
                else:
                    current_segment_factor = 1

                self.processed_parts = previously_processed_parts + self.sent_parts*current_segment_factor
                self.progress_total_parts = self.total_segments*max_parts_per_segment

        else:
            if not self.split:
                self.processed_parts = self.received_count
                self.progress_total_parts = float(self.total_parts)

            else:
                is_last_segment = self.segment_index != self.total_segments
                total_segments = self.total_segments
                processed_segments = self.segment_index-1

                current_segment_parts = self.total_parts
                max_parts_per_segment = math.ceil(Resource.MAX_EFFICIENT_SIZE/self.sdu)

                previously_processed_parts = processed_segments*max_parts_per_segment

                if current_segment_parts < max_parts_per_segment:
                    current_segment_factor = max_parts_per_segment / current_segment_parts
                else:
                    current_segment_factor = 1

                self.processed_parts = previously_processed_parts + self.received_count*current_segment_factor
                self.progress_total_parts = self.total_segments*max_parts_per_segment


        progress = min(1.0, self.processed_parts / self.progress_total_parts)
        return progress

    def get_segment_progress(self):
        if self.status == RNS.Resource.COMPLETE and self.segment_index == self.total_segments:
            return 1.0
        elif self.initiator:
            processed_parts = self.sent_parts
        else:
            processed_parts = self.received_count

        progress = min(1.0, processed_parts / self.total_parts)
        return progress

    def get_transfer_size(self):
        """
        :returns: The number of bytes needed to transfer the resource.
        """
        return self.size

    def get_data_size(self):
        """
        :returns: The total data size of the resource.
        """
        return self.total_size

    def get_parts(self):
        """
        :returns: The number of parts the resource will be transferred in.
        """
        return self.total_parts

    def get_segments(self):
        """
        :returns: The number of segments the resource is divided into.
        """
        return self.total_segments

    def get_hash(self):
        """
        :returns: The hash of the resource.
        """
        return self.hash

    def is_compressed(self):
        """
        :returns: Whether the resource is compressed.
        """
        return self.compressed

    def __str__(self):
        return "<"+RNS.hexrep(self.hash,delimit=False)+"/"+RNS.hexrep(self.link.link_id,delimit=False)+">"


class ResourceAdvertisement:
    OVERHEAD             = 134
    HASHMAP_MAX_LEN      = math.floor((RNS.Link.MDU-OVERHEAD)/Resource.MAPHASH_LEN)
    COLLISION_GUARD_SIZE = 2*Resource.WINDOW_MAX+HASHMAP_MAX_LEN

    assert HASHMAP_MAX_LEN > 0, "The configured MTU is too small to include any map hashes in resource advertisments"

    @staticmethod
    def is_request(advertisement_packet):
        adv = ResourceAdvertisement.unpack(advertisement_packet.plaintext)
        if adv.q != None and adv.u:
            return True
        else:
            return False


    @staticmethod
    def is_response(advertisement_packet):
        adv = ResourceAdvertisement.unpack(advertisement_packet.plaintext)

        if adv.q != None and adv.p:
            return True
        else:
            return False


    @staticmethod
    def read_request_id(advertisement_packet):
        adv = ResourceAdvertisement.unpack(advertisement_packet.plaintext)
        return adv.q


    @staticmethod
    def read_transfer_size(advertisement_packet):
        adv = ResourceAdvertisement.unpack(advertisement_packet.plaintext)
        return adv.t


    @staticmethod
    def read_size(advertisement_packet):
        adv = ResourceAdvertisement.unpack(advertisement_packet.plaintext)
        return adv.d


    def __init__(self, resource=None, request_id=None, is_response=False):
        self.link = None
        if resource != None:
            self.t = resource.size              # Transfer size
            self.d = resource.total_size        # Total uncompressed data size
            self.n = len(resource.parts)        # Number of parts
            self.h = resource.hash              # Resource hash
            self.r = resource.random_hash       # Resource random hash
            self.o = resource.original_hash     # First-segment hash
            self.m = resource.hashmap           # Resource hashmap
            self.c = resource.compressed        # Compression flag
            self.e = resource.encrypted         # Encryption flag
            self.s = resource.split             # Split flag
            self.x = resource.has_metadata      # Metadata flag
            self.i = resource.segment_index     # Segment index
            self.l = resource.total_segments    # Total segments
            self.q = resource.request_id        # ID of associated request
            self.u = False                      # Is request flag
            self.p = False                      # Is response flag

            if self.q != None:
                if not resource.is_response:
                    self.u = True
                    self.p = False
                else:
                    self.u = False
                    self.p = True

            # Flags
            self.f = 0x00 | self.x << 5 | self.p << 4 | self.u << 3 | self.s << 2 | self.c << 1 | self.e

    def get_transfer_size(self):
        return self.t

    def get_data_size(self):
        return self.d

    def get_parts(self):
        return self.n

    def get_segments(self):
        return self.l

    def get_hash(self):
        return self.h

    def is_compressed(self):
        return self.c

    def has_metadata(self):
        return self.x

    def get_link(self):
        return self.link

    def pack(self, segment=0):
        hashmap_start = segment*ResourceAdvertisement.HASHMAP_MAX_LEN
        hashmap_end   = min((segment+1)*(ResourceAdvertisement.HASHMAP_MAX_LEN), self.n)

        hashmap = b""
        for i in range(hashmap_start,hashmap_end):
            hashmap += self.m[i*Resource.MAPHASH_LEN:(i+1)*Resource.MAPHASH_LEN]

        dictionary = {
            "t": self.t,    # Transfer size
            "d": self.d,    # Data size
            "n": self.n,    # Number of parts
            "h": self.h,    # Resource hash
            "r": self.r,    # Resource random hash
            "o": self.o,    # Original hash
            "i": self.i,    # Segment index
            "l": self.l,    # Total segments
            "q": self.q,    # Request ID
            "f": self.f,    # Resource flags
            "m": hashmap
        }

        return umsgpack.packb(dictionary)


    @staticmethod
    def unpack(data):
        dictionary = umsgpack.unpackb(data)
        
        adv   = ResourceAdvertisement()
        adv.t = dictionary["t"]
        adv.d = dictionary["d"]
        adv.n = dictionary["n"]
        adv.h = dictionary["h"]
        adv.r = dictionary["r"]
        adv.o = dictionary["o"]
        adv.m = dictionary["m"]
        adv.f = dictionary["f"]
        adv.i = dictionary["i"]
        adv.l = dictionary["l"]
        adv.q = dictionary["q"]
        adv.e = True if (adv.f & 0x01) == 0x01 else False
        adv.c = True if ((adv.f >> 1) & 0x01) == 0x01 else False
        adv.s = True if ((adv.f >> 2) & 0x01) == 0x01 else False
        adv.u = True if ((adv.f >> 3) & 0x01) == 0x01 else False
        adv.p = True if ((adv.f >> 4) & 0x01) == 0x01 else False
        adv.x = True if ((adv.f >> 5) & 0x01) == 0x01 else False

        return adv