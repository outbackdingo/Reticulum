# MIT License
#
# Copyright (c) 2016-2025 Mark Qvist / unsigned.io
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

from RNS.Interfaces.Interface import Interface
import threading
import socket
import select
import time
import sys
import os
import RNS

class HDLC():
    FLAG              = 0x7E
    ESC               = 0x7D
    ESC_MASK          = 0x20

    @staticmethod
    def escape(data):
        data = data.replace(bytes([HDLC.ESC]), bytes([HDLC.ESC, HDLC.ESC^HDLC.ESC_MASK]))
        data = data.replace(bytes([HDLC.FLAG]), bytes([HDLC.ESC, HDLC.FLAG^HDLC.ESC_MASK]))
        return data

class BackboneInterface(Interface):
    HW_MTU            = 1048576
    BITRATE_GUESS     = 100_000_000
    DEFAULT_IFAC_SIZE = 16
    AUTOCONFIGURE_MTU = True

    @staticmethod
    def get_address_for_if(name, bind_port, prefer_ipv6=False):
        import RNS.vendor.ifaddr.niwrapper as netinfo
        ifaddr = netinfo.ifaddresses(name)
        if len(ifaddr) < 1:
            raise SystemError(f"No addresses available on specified kernel interface \"{name}\" for BackboneInterface to bind to")

        if (prefer_ipv6 or not netinfo.AF_INET in ifaddr) and netinfo.AF_INET6 in ifaddr:
            bind_ip = ifaddr[netinfo.AF_INET6][0]["addr"]
            if bind_ip.lower().startswith("fe80::"):
                # We'll need to add the interface as scope for link-local addresses
                return self.get_address_for_host(f"{bind_ip}%{name}", bind_port)
            else:
                return self.get_address_for_host(bind_ip, bind_port)
        elif netinfo.AF_INET in ifaddr:
            bind_ip = ifaddr[netinfo.AF_INET][0]["addr"]
            return (bind_ip, bind_port)
        else:
            raise SystemError(f"No addresses available on specified kernel interface \"{name}\" for BackboneInterface to bind to")

    @staticmethod
    def get_address_for_host(name, bind_port):
        address_info = socket.getaddrinfo(name, bind_port, proto=socket.IPPROTO_TCP)[0]
        if address_info[0] == socket.AF_INET6:
            return (name, bind_port, address_info[4][2], address_info[4][3])
        elif address_info[0] == socket.AF_INET:
            return (name, bind_port)
        else:
            raise SystemError(f"No suitable kernel interface available for address \"{name}\" for BackboneInterface to bind to")


    @property
    def clients(self):
        return len(self.spawned_interfaces)

    def __init__(self, owner, configuration):
        if not RNS.vendor.platformutils.is_linux() and not RNS.vendor.platformutils.is_android():
            raise OSError("BackboneInterface is only supported on Linux-based operating systems")

        super().__init__()

        c            = Interface.get_config_obj(configuration)
        name         = c["name"]
        device       = c["device"] if "device" in c else None
        port         = int(c["port"]) if "port" in c else None
        bindip       = c["listen_ip"] if "listen_ip" in c else None
        bindport     = int(c["listen_port"]) if "listen_port" in c else None
        prefer_ipv6  = c.as_bool("prefer_ipv6") if "prefer_ipv6" in c else False

        if port != None:
            bindport = port

        self.HW_MTU = BackboneInterface.HW_MTU

        self.online = False
        self.listeners = []
        self.spawned_interfaces = []
        
        self.IN  = True
        self.OUT = False
        self.name = name
        self.detached = False

        self.mode         = RNS.Interfaces.Interface.Interface.MODE_FULL

        if bindport == None:
            raise SystemError(f"No TCP port configured for interface \"{name}\"")
        else:
            self.bind_port = bindport

        bind_address = None
        if device != None:
            bind_address = self.get_address_for_if(device, self.bind_port, prefer_ipv6)
        else:
            if bindip == None:
                raise SystemError(f"No TCP bind IP configured for interface \"{name}\"")
            bind_address = self.get_address_for_host(bindip, self.bind_port)

        if bind_address != None:
            self.receives = True
            self.bind_ip = bind_address[0]
            self.owner = owner

            # if len(bind_address) == 4:
            #     try:
            #         ThreadingTCP6Server.allow_reuse_address = True
            #         self.server = ThreadingTCP6Server(bind_address, handlerFactory(self.incoming_connection))
            #     except Exception as e:
            #         RNS.log(f"Error while binding IPv6 socket for interface, the contained exception was: {e}", RNS.LOG_ERROR)
            #         raise SystemError("Could not bind IPv6 socket for interface. Please check the specified \"listen_ip\" configuration option")
            # else:

            self.epoll = select.epoll()
            self.add_listener(bind_address)
            self.bitrate = self.BITRATE_GUESS
            
            self.start()
            self.online = True

        else:
            raise SystemError("Insufficient parameters to create listener")

    def start(self):
        RNS.log(f"Starting {self}")
        threading.Thread(target=self.__job, daemon=True).start()

    def __job(self):
        try:
            while True:
                events = self.epoll.poll(1)

                for spawned_interface in self.spawned_interfaces:
                    clientsocket = spawned_interface.socket
                    for fileno, event in events:
                        if fileno == clientsocket.fileno() and (event & select.EPOLLIN):
                            try:
                                inb = clientsocket.recv(4096)
                            except Exception as e:
                                RNS.log(f"Error while reading from {spawned_interface}: {e}", RNS.LOG_ERROR)
                                inb = b""

                            if len(inb):
                                spawned_interface.receive(inb)
                            else:
                                self.epoll.unregister(fileno)
                                clientsocket.close()
                                spawned_interface.receive(inb)
                        
                        elif fileno == clientsocket.fileno() and (event & select.EPOLLOUT):
                            try:
                                written = clientsocket.send(spawned_interface.transmit_buffer)
                            except Exception as e:
                                RNS.log(f"Error while writing to {spawned_interface}: {e}", RNS.LOG_ERROR)
                                written = 0

                            spawned_interface.transmit_buffer = spawned_interface.transmit_buffer[written:]
                            if len(spawned_interface.transmit_buffer) == 0: self.epoll.modify(fileno, select.EPOLLIN)
                            self.txb += written; spawned_interface.txb += written
                        
                        elif fileno == clientsocket.fileno() and event & (select.EPOLLHUP):
                            self.epoll.unregister(fileno)
                            try: clientsocket.close()
                            except Exception as e:
                                RNS.log(f"Error while closing socket for {spawned_interface}: {e}", RNS.LOG_ERROR)

                            spawned_interface.receive(b"")

                for serversocket in self.listeners:
                    for fileno, event in events:
                        if fileno == serversocket.fileno(): RNS.log(f"Listener {serversocket}, fd {fileno}, event {event}")
                        if fileno == serversocket.fileno() and (event & select.EPOLLIN):
                            connection, address = serversocket.accept()
                            connection.setblocking(0)
                            if self.incoming_connection(connection):
                                self.epoll.register(connection.fileno(), select.EPOLLIN)
                            else:
                                connection.close()
                        
                        elif fileno == serversocket.fileno() and (event & select.EPOLLHUP):
                            try: self.epoll.unregister(fileno)
                            except Exception as e:
                                RNS.log(f"Error while unregistering file descriptor {fileno}: {e}", RNS.LOG_ERROR)

                            try: serversocket.close()
                            except Exception as e:
                                RNS.log(f"Error while closing socket for {serversocket}: {e}", RNS.LOG_ERROR)
        
        except Exception as e:
            RNS.log(f"{self} error: {e}", RNS.LOG_ERROR)
            RNS.trace_exception(e)

        finally:
            for serversocket in self.listeners:
                self.epoll.unregister(serversocket.fileno())
                serversocket.close()
            
            self.epoll.close()
    
    def add_listener(self, bind_address):
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind(bind_address)
        server_socket.listen(1)
        server_socket.setblocking(0)
        self.epoll.register(server_socket.fileno(), select.EPOLLIN)
        self.listeners.append(server_socket)
        RNS.log(f"Listener added: {server_socket}")

    def incoming_connection(self, socket):
        RNS.log("Accepting incoming connection", RNS.LOG_VERBOSE)
        spawned_configuration = {"name": "Client on "+self.name, "target_host": None, "target_port": None}
        spawned_interface = BackboneClientInterface(self.owner, spawned_configuration, connected_socket=socket)
        spawned_interface.OUT = self.OUT
        spawned_interface.IN  = self.IN
        spawned_interface.socket = socket
        spawned_interface.target_ip = socket.getpeername()[0]
        spawned_interface.target_port = str(socket.getpeername()[1])
        spawned_interface.parent_interface = self
        spawned_interface.bitrate = self.bitrate
        spawned_interface.optimise_mtu()
        
        spawned_interface.ifac_size = self.ifac_size
        spawned_interface.ifac_netname = self.ifac_netname
        spawned_interface.ifac_netkey = self.ifac_netkey
        if spawned_interface.ifac_netname != None or spawned_interface.ifac_netkey != None:
            ifac_origin = b""
            if spawned_interface.ifac_netname != None:
                ifac_origin += RNS.Identity.full_hash(spawned_interface.ifac_netname.encode("utf-8"))
            if spawned_interface.ifac_netkey != None:
                ifac_origin += RNS.Identity.full_hash(spawned_interface.ifac_netkey.encode("utf-8"))

            ifac_origin_hash = RNS.Identity.full_hash(ifac_origin)
            spawned_interface.ifac_key = RNS.Cryptography.hkdf(
                length=64,
                derive_from=ifac_origin_hash,
                salt=RNS.Reticulum.IFAC_SALT,
                context=None
            )
            spawned_interface.ifac_identity = RNS.Identity.from_bytes(spawned_interface.ifac_key)
            spawned_interface.ifac_signature = spawned_interface.ifac_identity.sign(RNS.Identity.full_hash(spawned_interface.ifac_key))

        spawned_interface.announce_rate_target = self.announce_rate_target
        spawned_interface.announce_rate_grace = self.announce_rate_grace
        spawned_interface.announce_rate_penalty = self.announce_rate_penalty
        spawned_interface.mode = self.mode
        spawned_interface.HW_MTU = self.HW_MTU
        spawned_interface.online = True
        RNS.log("Spawned new BackBoneClient Interface: "+str(spawned_interface), RNS.LOG_VERBOSE)
        RNS.Transport.interfaces.append(spawned_interface)
        while spawned_interface in self.spawned_interfaces:
            self.spawned_interfaces.remove(spawned_interface)
        self.spawned_interfaces.append(spawned_interface)

        return True

    def received_announce(self, from_spawned=False):
        if from_spawned: self.ia_freq_deque.append(time.time())

    def sent_announce(self, from_spawned=False):
        if from_spawned: self.oa_freq_deque.append(time.time())

    def process_outgoing(self, data):
        pass

    def detach(self):
        self.detached = True
        self.online = False
        for listener_socket in self.listeners:
            if hasattr(listener_socket, "shutdown"):
                if callable(listener_socket.shutdown):
                    try:
                        # RNS.log("Detaching "+str(self), RNS.LOG_DEBUG)
                        listener_socket.shutdown(socket.SHUT_RDWR)
                        
                    except Exception as e:
                        RNS.log("Error while shutting down server for "+str(self)+": "+str(e))

        while len(self.listeners): self.listeners.pop()

    def __str__(self):
        if ":" in self.bind_ip:
            ip_str = f"[{self.bind_ip}]"
        else:
            ip_str = f"{self.bind_ip}"

        return "BackboneInterface["+self.name+"/"+ip_str+":"+str(self.bind_port)+"]"


class BackboneClientInterface(Interface):
    BITRATE_GUESS = 100_000_000
    DEFAULT_IFAC_SIZE = 16
    AUTOCONFIGURE_MTU = True

    RECONNECT_WAIT = 5
    RECONNECT_MAX_TRIES = None

    # TCP socket options
    TCP_USER_TIMEOUT = 24
    TCP_PROBE_AFTER = 5
    TCP_PROBE_INTERVAL = 2
    TCP_PROBES = 12

    INITIAL_CONNECT_TIMEOUT = 5
    SYNCHRONOUS_START = True

    I2P_USER_TIMEOUT = 45
    I2P_PROBE_AFTER = 10
    I2P_PROBE_INTERVAL = 9
    I2P_PROBES = 5

    def __init__(self, owner, configuration, connected_socket=None):
        super().__init__()

        c = Interface.get_config_obj(configuration)
        name = c["name"]
        target_ip = c["target_host"] if "target_host" in c and c["target_host"] != None else None
        target_port = int(c["target_port"]) if "target_port" in c and c["target_host"] != None else None
        i2p_tunneled = c.as_bool("i2p_tunneled") if "i2p_tunneled" in c else False
        connect_timeout = c.as_int("connect_timeout") if "connect_timeout" in c else None
        max_reconnect_tries = c.as_int("max_reconnect_tries") if "max_reconnect_tries" in c else None
        
        self.HW_MTU           = BackboneInterface.HW_MTU
        self.IN               = True
        self.OUT              = False
        self.socket           = None
        self.parent_interface = None
        self.name             = name
        self.initiator        = False
        self.reconnecting     = False
        self.never_connected  = True
        self.owner            = owner
        self.online           = False
        self.detached         = False
        self.i2p_tunneled     = i2p_tunneled
        self.mode             = RNS.Interfaces.Interface.Interface.MODE_FULL
        self.bitrate          = BackboneClientInterface.BITRATE_GUESS
        self.frame_buffer     = b""
        self.transmit_buffer  = b""
        
        if max_reconnect_tries == None:
            self.max_reconnect_tries = BackboneClientInterface.RECONNECT_MAX_TRIES
        else:
            self.max_reconnect_tries = max_reconnect_tries

        if connected_socket != None:
            self.receives    = True
            self.target_ip   = None
            self.target_port = None
            self.socket      = connected_socket

            self.set_timeouts_linux()
            self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        elif target_ip != None and target_port != None:
            self.receives    = True
            self.target_ip   = target_ip
            self.target_port = target_port
            self.initiator   = True

            if connect_timeout != None:
                self.connect_timeout = connect_timeout
            else:
                self.connect_timeout = BackboneClientInterface.INITIAL_CONNECT_TIMEOUT
            
            if BackboneClientInterface.SYNCHRONOUS_START:
                self.initial_connect()
            else:
                thread = threading.Thread(target=self.initial_connect)
                thread.daemon = True
                thread.start()
            
    def initial_connect(self):
        if not self.connect(initial=True):
            thread = threading.Thread(target=self.reconnect)
            thread.daemon = True
            thread.start()
        else:
            thread = threading.Thread(target=self.read_loop)
            thread.daemon = True
            thread.start()
            self.wants_tunnel = True

    def set_timeouts_linux(self):
        self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT, int(BackboneClientInterface.TCP_USER_TIMEOUT * 1000))
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, int(BackboneClientInterface.TCP_PROBE_AFTER))
        self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, int(BackboneClientInterface.TCP_PROBE_INTERVAL))
        self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, int(BackboneClientInterface.TCP_PROBES))

    def detach(self):
        self.online = False
        if self.socket != None:
            if hasattr(self.socket, "close"):
                if callable(self.socket.close):
                    self.detached = True
                    
                    try:
                        if self.socket != None:
                            self.socket.shutdown(socket.SHUT_RDWR)
                    except Exception as e:
                        RNS.log("Error while shutting down socket for "+str(self)+": "+str(e))

                    try:
                        if self.socket != None:
                            self.socket.close()
                    except Exception as e:
                        RNS.log("Error while closing socket for "+str(self)+": "+str(e))

                    self.socket = None

    def connect(self, initial=False):
        try:
            if initial:
                RNS.log("Establishing TCP connection for "+str(self)+"...", RNS.LOG_DEBUG)

            address_info = socket.getaddrinfo(self.target_ip, self.target_port, proto=socket.IPPROTO_TCP)[0]
            address_family = address_info[0]
            target_address = address_info[4]

            self.socket = socket.socket(address_family, socket.SOCK_STREAM)
            self.socket.settimeout(BackboneClientInterface.INITIAL_CONNECT_TIMEOUT)
            self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.socket.connect(target_address)
            self.socket.settimeout(None)
            self.online  = True

            if initial:
                RNS.log("TCP connection for "+str(self)+" established", RNS.LOG_DEBUG)
        
        except Exception as e:
            if initial:
                RNS.log("Initial connection for "+str(self)+" could not be established: "+str(e), RNS.LOG_ERROR)
                RNS.log("Leaving unconnected and retrying connection in "+str(BackboneClientInterface.RECONNECT_WAIT)+" seconds.", RNS.LOG_ERROR)
                return False
            
            else:
                raise e

        self.set_timeouts_linux()
        
        self.online  = True
        self.never_connected = False

        return True


    def reconnect(self):
        if self.initiator:
            if not self.reconnecting:
                self.reconnecting = True
                attempts = 0
                while not self.online:
                    time.sleep(BackboneClientInterface.RECONNECT_WAIT)
                    attempts += 1

                    if self.max_reconnect_tries != None and attempts > self.max_reconnect_tries:
                        RNS.log("Max reconnection attempts reached for "+str(self), RNS.LOG_ERROR)
                        self.teardown()
                        break

                    try:
                        self.connect()

                    except Exception as e:
                        RNS.log("Connection attempt for "+str(self)+" failed: "+str(e), RNS.LOG_DEBUG)

                if not self.never_connected:
                    RNS.log("Reconnected socket for "+str(self)+".", RNS.LOG_INFO)

                self.reconnecting = False
                thread = threading.Thread(target=self.read_loop)
                thread.daemon = True
                thread.start()
                RNS.Transport.synthesize_tunnel(self)

        else:
            RNS.log("Attempt to reconnect on a non-initiator TCP interface. This should not happen.", RNS.LOG_ERROR)
            raise IOError("Attempt to reconnect on a non-initiator TCP interface")

    def process_incoming(self, data):
        if self.online and not self.detached:
            self.rxb += len(data)
            if hasattr(self, "parent_interface") and self.parent_interface != None:
                self.parent_interface.rxb += len(data)
                        
            self.owner.inbound(data, self)

    def process_outgoing(self, data):
        if self.online and not self.detached:
            try:
                self.transmit_buffer += bytes([HDLC.FLAG])+HDLC.escape(data)+bytes([HDLC.FLAG])
                if hasattr(self, "parent_interface") and self.parent_interface != None:
                    self.parent_interface.epoll.modify(self.socket.fileno(), select.EPOLLOUT)

            except Exception as e:
                RNS.log("Exception occurred while transmitting via "+str(self)+", tearing down interface", RNS.LOG_ERROR)
                RNS.log("The contained exception was: "+str(e), RNS.LOG_ERROR)
                self.teardown()

    def receive(self, data_in):
        try:
            if len(data_in) > 0:
                self.frame_buffer += data_in
                flags_remaining = True
                while flags_remaining:
                    frame_start = self.frame_buffer.find(HDLC.FLAG)
                    if frame_start != -1:
                        frame_end = self.frame_buffer.find(HDLC.FLAG, frame_start+1)
                        if frame_end != -1:
                            frame = self.frame_buffer[frame_start+1:frame_end]
                            frame = frame.replace(bytes([HDLC.ESC, HDLC.FLAG ^ HDLC.ESC_MASK]), bytes([HDLC.FLAG]))
                            frame = frame.replace(bytes([HDLC.ESC, HDLC.ESC  ^ HDLC.ESC_MASK]), bytes([HDLC.ESC]))
                            if len(frame) > RNS.Reticulum.HEADER_MINSIZE:
                                self.process_incoming(frame)
                            self.frame_buffer = self.frame_buffer[frame_end:]
                        else:
                            flags_remaining = False
                    else:
                        flags_remaining = False

            else:
                self.online = False
                if self.initiator and not self.detached:
                    RNS.log("The socket for "+str(self)+" was closed, attempting to reconnect...", RNS.LOG_WARNING)
                    self.reconnect()
                else:
                    RNS.log("The socket for remote client "+str(self)+" was closed.", RNS.LOG_VERBOSE)
                    self.teardown()
                
        except Exception as e:
            self.online = False
            RNS.log("An interface error occurred for "+str(self)+", the contained exception was: "+str(e), RNS.LOG_WARNING)

            if self.initiator:
                RNS.log("Attempting to reconnect...", RNS.LOG_WARNING)
                self.reconnect()
            else:
                self.teardown()

    def teardown(self):
        if self.initiator and not self.detached:
            RNS.log("The interface "+str(self)+" experienced an unrecoverable error and is being torn down. Restart Reticulum to attempt to open this interface again.", RNS.LOG_ERROR)
            if RNS.Reticulum.panic_on_interface_error:
                RNS.panic()

        else:
            RNS.log("The interface "+str(self)+" is being torn down.", RNS.LOG_VERBOSE)

        self.online = False
        self.OUT = False
        self.IN = False

        if hasattr(self, "parent_interface") and self.parent_interface != None:
            while self in self.parent_interface.spawned_interfaces:
                self.parent_interface.spawned_interfaces.remove(self)

        if self in RNS.Transport.interfaces:
            if not self.initiator:
                RNS.Transport.interfaces.remove(self)


    def __str__(self):
        if ":" in self.target_ip:
            ip_str = f"[{self.target_ip}]"
        else:
            ip_str = f"{self.target_ip}"

        return "BackboneInterface["+str(self.name)+"/"+ip_str+":"+str(self.target_port)+"]"