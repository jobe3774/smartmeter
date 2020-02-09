#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#  This application reads out the current values of my two energy meters.
#
#  The first one is a EBZ DD3 smart meter, which comes with a D0 interface pushing data every second. 
#  This optical data interface is a unidirectional communication interface using infrared light.
#  The data is read via an infrared read device which is attached to the so called 'Info-DSS' of the smart meter. 
#  The other end of the device is connect to one of the RPi's USB ports. 
#  DD3's data is ASCII and is specified in 'DIN EN 625056-21'.
#
#  The second one is a Finder Series 7E smart meter, which comes with a S0 interface (https://de.wikipedia.org/wiki/S0-Schnittstelle)
#  specified in 'DIN 43864'. I connected the S0+ of the smart meter to one of the RPi's 5V+ GPIO pins and S0- is connected to another
#  GPIO pin configured as an input pin. Since the minimum voltage for the S0 interface is 5V I had to use a voltage divider. 
#  I used a 2K and a 3K Ohm resistor to implement it, so the GPIO pin has a maximum voltage of 3V. 
#  The smart meter outputs 1000 pulses per kWh on the interface. These pulses are detected as rising edges on the GPIO pin and counted.
#  
#  License: MIT
#  
#  Copyright (c) 2020 Joerg Beckers

import RPi.GPIO as GPIO
import logging
import json
import os
import argparse
import serial
import re
from tzlocal import get_localzone
from datetime import datetime, timedelta, time, timezone
from raspend import RaspendApplication, ThreadHandlerBase
from collections import namedtuple

class SmartMeterKeys:
    """ OBIS codes of the EBZ DD3.
    """
    POWER_IMPORT = "1.8.0"
    POWER_EXPORT = "2.8.0"
    CURRENT_POWER_SUM = "16.7.0"
    CURRENT_POWER_L1 = "36.7.0"
    CURRENT_POWER_L2 = "56.7.0"
    CURRENT_POWER_L3 = "76.7.0"

class SmartMeterConstants:
    """ Some constants used for identifying begin and end of a datagram.
    """
    DATAGRAM_INITIATOR = '/'
    DATAGRAM_TERMINATOR = '!'

class ReadSmartMeter(ThreadHandlerBase):
    """ This class reads the datagrams of the EBZ DD3 from the USB device attached to the 'Info-DSS' of the smart meter.
    """
    def __init__(self, sectionName, serialPort, localTimeZone):
        self.sectionName = sectionName
        self.serialPort = serialPort
        self.localTimeZone = localTimeZone
        self.datagramBuffer = list()
        self.OBISCodeMap = dict()
        self.OBISCodeMap[SmartMeterKeys.POWER_IMPORT] = "POWER_IMPORT"
        self.OBISCodeMap[SmartMeterKeys.POWER_EXPORT] = "POWER_EXPORT"
        self.OBISCodeMap[SmartMeterKeys.CURRENT_POWER_SUM] = "CURRENT_POWER_SUM"
        self.OBISCodeMap[SmartMeterKeys.CURRENT_POWER_L1] = "CURRENT_POWER_L1"
        self.OBISCodeMap[SmartMeterKeys.CURRENT_POWER_L2] = "CURRENT_POWER_L2"
        self.OBISCodeMap[SmartMeterKeys.CURRENT_POWER_L3] = "CURRENT_POWER_L3"
        return 

    def extractSmartMeterValues(self, datagram):
        """ This method extracts only the relevant parts of the datagram and writes them into the shared dictionary.
        """ 
        regex = r"1-0:(\d+.[8|7].0)\*255\((-?\d+.\d+)\*(\w+)\)"
        matches = re.finditer(regex, datagram)
        thisDict = self.sharedDict[self.sectionName]
        thisDict["timestampUTC"] = datetime.now(timezone.utc).isoformat()
        for match in matches:
            strOBISCode = match.group(1)
            if strOBISCode in self.OBISCodeMap:
                thisDict[self.OBISCodeMap[strOBISCode]] = {"OBIS_Code": strOBISCode, "value": round(float(match.group(2)), 3), "unit" : match.group(3)}
        return 

    def prepare(self):
        """ Open the connected USB device for reading.
        """
        if not self.sectionName in self.sharedDict:
            self.sharedDict[self.sectionName] = dict()

        self.serial = serial.Serial(self.serialPort,
                                    baudrate = 9600,
                                    parity=serial.PARITY_EVEN,
                                    stopbits=serial.STOPBITS_ONE,
                                    bytesize=serial.SEVENBITS,
                                    timeout=1)
        return 

    def invoke(self):
        """ Reads one datagram per invocation. 
            Since the smart meter pushes one datagram every second, this should be the minimal timeout for this method.
            Currently this method is invoked every 5 seconds.
        """
        readDatagram = True
        beginDatagram = False
        endDatagram = False
        self.datagramBuffer.clear()

        while not self.aborted() and readDatagram:
            c = self.serial.read().decode("utf-8")

            if c == SmartMeterConstants.DATAGRAM_INITIATOR:
                beginDatagram = True
                endDatagram = False

            if c == SmartMeterConstants.DATAGRAM_TERMINATOR and beginDatagram:
                beginDatagram = False
                endDatagram = True

            if beginDatagram and not endDatagram:
                self.datagramBuffer.append(c)

            if endDatagram and not beginDatagram:
                self.datagramBuffer.append(c)
                self.extractSmartMeterValues(''.join(self.datagramBuffer))
                readDatagram = beginDatagram = endDatagram = False

        return

class S0InterfaceReader():
    """ This class counts the pulses of the Finder smart meter.
        On every rising edge detected, the GPIO interface invokes the ISR method below.
    """
    def __init__(self, sectionName, sharedDict, accessLock):
        self.sectionName = sectionName
        self.sharedDict = sharedDict
        if sectionName not in self.sharedDict:
            self.sharedDict[sectionName] = {"count" : 0.0, "timestampUTC": datetime.now(timezone.utc).isoformat()}
        self.accessLock = accessLock

    def setValue(self, value):
        """ This method is used to set the initial counter value of the smart meter. 
        """
        success = False
        self.accessLock.acquire()
        try:
            thisDict = self.sharedDict[self.sectionName]
            thisDict["count"] = float(value)
            thisDict["timestampUTC"] = datetime.now(timezone.utc).isoformat()
            success = True
        except Exception as e:
            print(e)
        finally:
            self.accessLock.release()
        return success

    def ISR(self, channel):  
        """ This is the interrupt service routine invoked by the GPIO interface when a rising edge has been detected.
        """
        self.accessLock.acquire()
        try:
            thisDict = self.sharedDict[self.sectionName]
            thisDict["count"] = thisDict["count"] + 0.001
            thisDict["timestampUTC"] = datetime.now(timezone.utc).isoformat()
        except Exception as e:
            print (e)
        finally:
            self.accessLock.release()

def main():
    localTimeZone = get_localzone()

    logging.basicConfig(filename='smartmeter.log', level=logging.INFO)

    logging.info("Starting at {} (PID={})".format(datetime.now(localTimeZone), os.getpid()))

    # Check commandline arguments.
    cmdLineParser = argparse.ArgumentParser(prog="smartmeter", usage="%(prog)s [options]")
    cmdLineParser.add_argument("--port", help="The port number the server should listen on", type=int, required=True)
    cmdLineParser.add_argument("--serialPort", help="The serial port to read from", type=str, required=True)
    cmdLineParser.add_argument("--s0Pin", help="The BCM number of the pin connected to the S0 interface", type=int, required=False)

    try: 
        args = cmdLineParser.parse_args()
    except SystemExit:
        return

    try:
        myApp = RaspendApplication(args.port)

        myApp.createWorkerThread(ReadSmartMeter("smartmeter_d0", args.serialPort, localTimeZone), 5)

        s0Interface = S0InterfaceReader("smartmeter_s0", myApp.getSharedDict(), myApp.getAccessLock())

        if args.s0Pin is not None:
            # Making this method available as a command enables us to set the initial value via HTTP GET.
            # http://<IP-OF-YOUR-RPI>:<PORT>/cmd?name=s0Interface.setValue&value=<COUNT>
            myApp.addCommand(s0Interface.setValue);

            # Setup the GPIO pin for detecting rising edges.
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(args.s0Pin, GPIO.IN, pull_up_down = GPIO.PUD_DOWN)
            GPIO.add_event_detect(args.s0Pin, GPIO.RISING, callback = s0Interface.ISR, bouncetime = 200)

        myApp.run()

        logging.info("Stopped at {} (PID={})".format(datetime.now(localTimeZone), os.getpid()))

    except Exception as e:
        logging.exception("Unexpected error occured!", exc_info = True)
    finally:
        if args.s0Pin is not None:
            GPIO.cleanup()

if __name__ == "__main__":
    main()