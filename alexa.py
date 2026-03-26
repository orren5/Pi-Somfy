#!/usr/bin/env python

"""
The MIT License (MIT)

Copyright (c) 2015 Maker Musings

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""

# For a complete discussion, see http://www.makermusings.com
# TODO(semartin): investigate time.sleep usage in here...
#
# Original standalone CLI config (fauxmo_config.json) for reference:
#   {"FAUXMO": {"ip_address": "auto"},
#    "PLUGINS": {"SimpleHTTPPlugin": {"DEVICES": [
#      {"name": "SOMFY",
#       "on_cmd": "http://localhost:80/cmd/stop?shutter={SHUTTER_ID}",
#       "off_cmd": "http://localhost:80/cmd/up?shutter={SHUTTER_ID}",
#       "method": "POST", "initial_state": "on", "use_fake_state": true}]}}}
# This config is not used; the Alexa class at the bottom of this module
# imports the fauxmo classes directly.

import email.utils
import os
import requests
import select
import socket
import struct
import sys
import time
import threading
import urllib
import uuid

try:
    from config import MyLog
except Exception as e1:
    print("\n\nThis program requires the modules located from the same github repository that are not present.\n")
    print("Error: " + str(e1))
    sys.exit(2)

# This XML is the minimum needed to define one of our virtual switches
# to the Amazon Echo

SETUP_XML ="""<?xml version="1.0"?>
            <root>
             <device>
                <deviceType>urn:Belkin:device:controllee:1</deviceType>
                <friendlyName>%(device_name)s</friendlyName>
                <manufacturer>Belkin International Inc.</manufacturer>
                <modelName>Socket</modelName>
                <modelNumber>3.1415</modelNumber>
                <modelDescription>Belkin Plugin Socket 1.0</modelDescription>
                <UDN>uuid:Socket-1_0-%(device_serial)s</UDN>
                <serialNumber>221517K0101769</serialNumber>
                <binaryState>0</binaryState>
                <serviceList>
                  <service>
                      <serviceType>urn:Belkin:service:basicevent:1</serviceType>
                      <serviceId>urn:Belkin:serviceId:basicevent1</serviceId>
                      <controlURL>/upnp/control/basicevent1</controlURL>
                      <eventSubURL>/upnp/event/basicevent1</eventSubURL>
                      <SCPDURL>/eventservice.xml</SCPDURL>
                  </service>
              </serviceList> 
              </device>
            </root>"""


# A simple utility class to wait for incoming data to be
# ready on a socket.

class poller (MyLog):
    def __init__(self, log):
        self.targets = {}
        self.log = log
        # select.poll() is not available on Windows; fall back to select.select()
        self._use_poll = hasattr(select, 'poll')
        if self._use_poll:
            self._poller = select.poll()
        else:
            self._sockets = {}  # fileno -> socket object (needed for select.select on Windows)

    def add(self, target, fileno = None):
        if not fileno:
            fileno = target.fileno()
        if self._use_poll:
            self._poller.register(fileno, select.POLLIN)
        else:
            # On Windows, select.select() needs socket objects, not ints
            sock = target.socket if hasattr(target, 'socket') else target.ssock if hasattr(target, 'ssock') else None
            if fileno != target.fileno() and hasattr(target, 'client_sockets') and fileno in target.client_sockets:
                sock = target.client_sockets[fileno][0]
            if sock is not None:
                self._sockets[fileno] = sock
        self.targets[fileno] = target

    def remove(self, target, fileno = None):
        if not fileno:
            fileno = target.fileno()
        if self._use_poll:
            self._poller.unregister(fileno)
        else:
            self._sockets.pop(fileno, None)
        del(self.targets[fileno])

    def poll(self, timeout = 0):
        if self._use_poll:
            ready = self._poller.poll(timeout)
            num = len(ready)
            for one_ready in ready:
                target = self.targets.get(one_ready[0], None)
                if target:
                    target.do_read(one_ready[0])
        else:
            # Windows fallback using select.select() with socket objects
            socks = list(self._sockets.values())
            if not socks:
                return 0
            readable, _, _ = select.select(socks, [], [], timeout / 1000.0 if timeout else 0)
            num = len(readable)
            for sock in readable:
                fileno = sock.fileno()
                target = self.targets.get(fileno, None)
                if target:
                    target.do_read(fileno)
        return num


# Base class for a generic UPnP device. This is far from complete
# but it supports either specified or automatic IP address and port
# selection.

class upnp_device(MyLog, object):
    this_host_ip = None

    @staticmethod
    def local_ip_address():
        if not upnp_device.this_host_ip:
            temp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                temp_socket.connect(('8.8.8.8', 53))
                upnp_device.this_host_ip = temp_socket.getsockname()[0]
            except:
                upnp_device.this_host_ip = '127.0.0.1'
            del(temp_socket)
            # self.LogInfo("got local address of %s" % upnp_device.this_host_ip)
        return upnp_device.this_host_ip


    def __init__(self, listener, poller, port, root_url, server_version, persistent_uuid, other_headers = None, ip_address = None, log = None):
        self.listener = listener
        self.poller = poller
        self.port = port
        self.root_url = root_url
        self.server_version = server_version
        self.persistent_uuid = persistent_uuid
        self.uuid = uuid.uuid4()
        self.other_headers = other_headers
        if (log != None): 
            self.log = log

        if ip_address:
            self.ip_address = ip_address
        else:
            self.ip_address = upnp_device.local_ip_address()

        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind((self.ip_address, self.port))
        self.socket.listen(5)
        if self.port == 0:
            self.port = self.socket.getsockname()[1]
        self.poller.add(self)
        self.client_sockets = {}
        self.listener.add_device(self)

    def fileno(self):
        return self.socket.fileno()

    def do_read(self, fileno):
        if fileno == self.socket.fileno():
            (client_socket, client_address) = self.socket.accept()
            self.poller.add(self, client_socket.fileno())
            self.client_sockets[client_socket.fileno()] = (client_socket, client_address)
        else:
            data, sender = self.client_sockets[fileno][0].recvfrom(4096)
            if not data:
                self.poller.remove(self, fileno)
                del(self.client_sockets[fileno])
            else:
                self.handle_request(data, sender, self.client_sockets[fileno][0], self.client_sockets[fileno][1])

    def handle_request(self, data, sender, socket, client_address):
        pass

    def get_name(self):
        return "unknown"

    def respond_to_search(self, destination, search_target):
        # self.LogDebug("Responding to search for %s" % self.get_name())
        date_str = email.utils.formatdate(timeval=None, localtime=False, usegmt=True)
        location_url = self.root_url % {'ip_address' : self.ip_address, 'port' : self.port}
        message = ("HTTP/1.1 200 OK\r\n"
                  "CACHE-CONTROL: max-age=86400\r\n"
                  "DATE: %s\r\n"
                  "EXT:\r\n"
                  "LOCATION: %s\r\n"
                  "OPT: \"http://schemas.upnp.org/upnp/1/0/\"; ns=01\r\n"
                  "01-NLS: %s\r\n"
                  "SERVER: %s\r\n"
                  "ST: %s\r\n"
                  "USN: uuid:%s::%s\r\n" % (date_str, location_url, self.uuid, self.server_version, search_target, self.persistent_uuid, search_target))
        if self.other_headers:
            for header in self.other_headers:
                message += "%s\r\n" % header
        message += "\r\n"
        temp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            for _ in range(3):
                temp_socket.sendto(bytes(message, 'UTF-8'), destination)
                time.sleep(0.1)
        finally:
            temp_socket.close()

# This subclass does the bulk of the work to mimic a WeMo switch on the network.

class fauxmo(upnp_device):
    @staticmethod
    def make_uuid(name):
        return ''.join(["%x" % sum([ord(c) for c in name])] + ["%x" % ord(c) for c in "%sfauxmo!" % name])[:14]

    def __init__(self, name, listener, poller, ip_address, port, action_handler = None, log = None):
        self.serial = self.make_uuid(name)
        self.name = name
        self.switchStatus=0
        self.ip_address = ip_address
        if (log != None):
            self.log = log
        persistent_uuid = "Socket-1_0-" + self.serial
        other_headers = ['X-User-Agent: redsonic']
        upnp_device.__init__(self, listener, poller, port, "http://%(ip_address)s:%(port)s/setup.xml", "Unspecified, UPnP/1.0, Unspecified", persistent_uuid, other_headers=other_headers, ip_address=ip_address, log=self.log)
        if action_handler:
            self.action_handler = action_handler
        else:
            self.action_handler = self
        self.LogInfo("FauxMo device '%s' ready on %s:%s" % (self.name, self.ip_address, self.port))

    def get_name(self):
        return self.name

    def handle_request(self, data, sender, socket, client_address):
        # self.LogDebug("################################## BEGIN  handle_request #######################")
        self.LogDebug("HANDLE REQUEST: "+str(data))
        # self.LogDebug("################################## END    handle_request #######################")
        data = data.decode('utf-8')
        success = False
        
        if data.find('GET /setup.xml HTTP/1.1') == 0:
            self.LogInfo("Responding to setup.xml for %s" % self.name)
            xml = SETUP_XML % {'device_name' : self.name, 'device_serial' : self.serial}
            date_str = email.utils.formatdate(timeval=None, localtime=False, usegmt=True)
            message = ("HTTP/1.1 200 OK\r\n"
                       "CONTENT-LENGTH: %d\r\n"
                       "CONTENT-TYPE: text/xml\r\n"
                       "DATE: %s\r\n"
                       "LAST-MODIFIED: Sat, 01 Jan 2000 00:01:15 GMT\r\n"
                       "SERVER: Unspecified, UPnP/1.0, Unspecified\r\n"
                       "X-User-Agent: redsonic\r\n"
                       "CONNECTION: close\r\n"
                       "\r\n"
                       "%s" % (len(xml), date_str, xml))
            socket.send(bytes(message, 'UTF-8'))
            #print("responsed to setup-->" + message)
        
        elif data.find('SOAPACTION: "urn:Belkin:service:basicevent:1#SetBinaryState"') != -1:
        #elif data.find('urn:Belkin:service:basicevent:1') != -1:
        #elif data.find("SetBinaryState") != -1:
            
            if data.find('SetBinaryState') != -1:
                if data.find('<BinaryState>1</BinaryState>') != -1:
                    # on
                    self.LogInfo("Responding to ON for %s" % self.name)
                    success = self.action_handler.on(client_address[0], self.name)
                    self.switchStatus=1
                elif data.find('<BinaryState>0</BinaryState>') != -1:
                    # off
                    self.LogInfo("Responding to OFF for %s" % self.name)
                    success = self.action_handler.off(client_address[0], self.name)
                    self.switchStatus=0
                else:
                    self.LogInfo("Unknown Binary State request:")
                    self.LogInfo(data)
                                
            if success:
                # The echo is happy with the 200 status code and doesn't
                # appear to care about the SOAP response body
                #self.LogInfo("Unknown Binary State request:")
                soap = "" 
                date_str = email.utils.formatdate(timeval=None, localtime=False, usegmt=True)
                message = ("HTTP/1.1 200 OK\r\n"
                           "CONTENT-LENGTH: %d\r\n"
                           "CONTENT-TYPE: text/xml charset=\"utf-8\"\r\n"
                           "DATE: %s\r\n"
                           "EXT:\r\n"
                           "SERVER: Unspecified, UPnP/1.0, Unspecified\r\n"
                           "X-User-Agent: redsonic\r\n"
                           "CONNECTION: close\r\n"
                           "\r\n"
                           "%s" % (len(soap), date_str, soap))
                socket.send(bytes(message, 'UTF-8'))
                
        elif data.find('GetBinaryState') != -1:
            #if data.find('<BinaryState>1</BinaryState>') != -1:
            #    switch_sate="1"
            #else:
            #    switch_sate="0"
            soap = """<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
                <s:Body>
                    <u:GetBinaryStateResponse
                    xmlns:u="urn:Belkin:service:basicevent:1">
                    <BinaryState>"""+ str(self.switchStatus) +"""</BinaryState>
                    </u:GetBinaryStateResponse>
                </s:Body></s:Envelope>""" 
            
            
            date_str = email.utils.formatdate(timeval=None, localtime=False, usegmt=True)
            message = ("HTTP/1.1 200 OK\r\n"
                       "CONTENT-LENGTH: %d\r\n"
                       "CONTENT-TYPE: text/xml charset=\"utf-8\"\r\n"
                       "DATE: %s\r\n"
                       "EXT:\r\n"
                       "SERVER: Unspecified, UPnP/1.0, Unspecified\r\n"
                       "X-User-Agent: redsonic\r\n"
                       "CONNECTION: close\r\n"
                       "\r\n"
                       "%s" % (len(soap), date_str, soap))
            socket.send(bytes(message, 'UTF-8'))
            # self.LogDebug("################################## BEGIN response #######################")
            self.LogDebug("SEND RESPONSE: "+str(data.replace('\n','\\n').replace('\r','\\r')))
            # self.LogDebug("################################## END response #######################")

        else:
            self.LogInfo(data)

    def on(self):
        return False

    def off(self):
        return True


# Since we have a single process managing several virtual UPnP devices,
# we only need a single listener for UPnP broadcasts. When a matching
# search is received, it causes each device instance to respond.
#
# Note that this is currently hard-coded to recognize only the search
# from the Amazon Echo for WeMo devices. In particular, it does not
# support the more common root device general search. The Echo
# doesn't search for root devices.

class upnp_broadcast_responder(MyLog, object):
    TIMEOUT = 0

    def __init__(self, log):
        self.devices = []
        self.log = log

    def init_socket(self):
        ok = True
        self.ip = '239.255.255.250'
        self.port = 1900
        try:
            #This is needed to join a multicast group
            self.mreq = struct.pack("4sl",socket.inet_aton(self.ip),socket.INADDR_ANY)

            #Set up server socket
            self.ssock = socket.socket(socket.AF_INET,socket.SOCK_DGRAM,socket.IPPROTO_UDP)
            self.ssock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)

            try:
                self.ssock.bind(('',self.port))
            except Exception:
                self.LogWarn("WARNING: Failed to bind %s:%d" % (self.ip,self.port))
                ok = False

            try:
                self.ssock.setsockopt(socket.IPPROTO_IP,socket.IP_ADD_MEMBERSHIP,self.mreq)
            except Exception:
                self.LogWarn('WARNING: Failed to join multicast group:')
                ok = False

        except Exception:
            self.LogInfo("Failed to initialize UPnP sockets:")
            return False
        if ok:
            self.LogInfo("Listening for UPnP broadcasts")

    def fileno(self):
        return self.ssock.fileno()

    def do_read(self, fileno):
        data, sender = self.recvfrom(1024)
        data = data.decode('utf-8')
        if data:
            if data.find('M-SEARCH') >= 0 and (data.find('urn:Belkin:device:**') > 0 or data.find('upnp:rootdevice') > 0):
                for device in self.devices:
                    time.sleep(0.5)
                    device.respond_to_search(sender, 'urn:Belkin:device:**')
            else:
                pass

    #Receive network data
    def recvfrom(self,size):
        if self.TIMEOUT:
            self.ssock.setblocking(0)
            ready = select.select([self.ssock], [], [], self.TIMEOUT)[0]
        else:
            self.ssock.setblocking(1)
            ready = True

        try:
            if ready:
                return self.ssock.recvfrom(size)
            else:
                return False, False
        except Exception:
            self.LogError('Error: exception occurred in recvfrom')
            return False, False

    def add_device(self, device):
        self.devices.append(device)
        self.LogInfo("UPnP broadcast listener: new device registered")


class debounce_handler(object):
    """Use this handler to keep multiple Amazon Echo devices from reacting to
       the same voice command.
    """
    DEBOUNCE_SECONDS = 0.3

    def __init__(self):
        self.lastEcho = time.time()

    def on(self, client_address, name):
        if self.debounce():
            return True
        return self.act(client_address, True, name)

    def off(self, client_address, name):
        if self.debounce():
            return True
        return self.act(client_address, False, name)

    def act(self, client_address, state):
        pass

    def debounce(self):
        """If multiple Echos are present, the one most likely to respond first
           is the one that can best hear the speaker... which is the closest one.
           Adding a refractory period to handlers keeps us from worrying about
           one Echo overhearing a command meant for another one.
        """
        if (time.time() - self.lastEcho) < self.DEBOUNCE_SECONDS:
            return True

        self.lastEcho = time.time()
        return False


##############################################################################################
#### BASED ON WORK BY : https://github.com/nassir-malik/IOT-Pi3-Alexa-Automation          ####
##############################################################################################

class device_handler(debounce_handler, MyLog):
    """Publishes the on/off state requested,
       and the IP address of the Echo making the request.
    """
    def __init__(self, log=None, shutter=None, config=None):
        self.log = log
        self.shutter = shutter
        self.config = config
        super(device_handler, self).__init__()        
    
    def act(self, client_address, state, name):
        self.LogInfo("--> State " + str(state) + " on " + name + " from client @ " + client_address)
        shutterId = self.config.ShuttersByName[name]
        if state:
           self.shutter.lower(shutterId)
        else:
           self.shutter.rise(shutterId)
        return True


class Alexa(threading.Thread, MyLog):

    def __init__(self, group=None, target=None, name=None, args=(), kwargs=None):
        threading.Thread.__init__(self, group=group, target=target, name="Alexa")
        self.shutdown_flag = threading.Event()
        
        self.args = args
        self.kwargs = kwargs
        if kwargs["log"] != None:
            self.log = kwargs["log"]
        if kwargs["shutter"] != None:
            self.shutter = kwargs["shutter"]
        if kwargs["config"] != None:
            self.config = kwargs["config"]
        
        # Startup the fauxmo server
        self.poller = poller(log = self.log)
        self.upnp_responder = upnp_broadcast_responder(log = self.log)
        self.upnp_responder.init_socket()
        self.poller.add(self.upnp_responder)

        # Register the device callback as a fauxmo handler
        self.dbh = device_handler(log=self.log, shutter=self.shutter, config=self.config)
        self.fauxmo_devices = {}  # name -> fauxmo instance
        for shutter, shutterId in sorted(self.config.ShuttersByName.items(), key=lambda kv: kv[1]):
            self._register_device(shutter, shutterId)

        return

    def _register_device(self, name, shutterId):
        portId = 50000 + (abs(int(shutterId, 16)) % 10000)
        self.LogInfo("Remote address in dec: " + str(int(shutterId, 16)) + ", WeMo port will be n°" + str(portId))
        dev = fauxmo(name, self.upnp_responder, self.poller, None, portId, self.dbh, log=self.log)
        self.fauxmo_devices[name] = dev

    def _sync_devices(self):
        """Register/deregister fauxmo devices to match current config."""
        current = set(self.config.ShuttersByName.keys())
        registered = set(self.fauxmo_devices.keys())

        # Register new shutters
        for name in current - registered:
            shutterId = self.config.ShuttersByName[name]
            self.LogInfo("New shutter detected, registering Alexa device: " + name)
            self._register_device(name, shutterId)

        # Deregister removed shutters
        for name in registered - current:
            self.LogInfo("Shutter removed, deregistering Alexa device: " + name)
            dev = self.fauxmo_devices.pop(name)
            # Remove device TCP socket from poller
            self.poller.remove(dev)
            # Remove from UPnP responder device list
            if dev in self.upnp_responder.devices:
                self.upnp_responder.devices.remove(dev)
            try:
                dev.socket.close()
            except Exception:
                pass

    def run(self):
        self.LogInfo("Entering fauxmo polling loop")
        error = 0
        sync_counter = 0
        while not self.shutdown_flag.is_set():
            # Loop and poll for incoming Echo requests
            try:
                # Allow time for a ctrl-c to stop the process
                self.poller.poll(100)
                time.sleep(0.01)
                # Check for new/removed shutters every ~5 seconds
                sync_counter += 1
                if sync_counter >= 50:
                    sync_counter = 0
                    self._sync_devices()
            except Exception as e:
                error += 1
                self.LogInfo("Critical exception n°" + str(error) + ": "+ str(e.args))
                print("Trying not to shut down Alexa")
                time.sleep(0.5) #Wait half a second when an exception occurs


