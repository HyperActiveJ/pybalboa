import asyncio
import errno
import logging
import time
import warnings
import queue
import socket
from socket import error as SocketError
from datetime import datetime 
try:
    from balboa import *
except:
    from .balboa import *

#Common to all known Balboa Products
CLIENT_CLEAR_TO_SEND = 0x00
CHANNEL_ASSIGNMENT_REQ = 0x01
CHANNEL_ASSIGNMENT_RESPONCE = 0x02
CHANNEL_ASSIGNMENT_ACK = 0x03
EXISTING_CLIENT_REQ = 0x04
EXISTING_CLIENT_RESPONCE = 0x05
CLEAR_TO_SEND = 0x06
NOTHING_TO_SEND =  0x07

#These were found on a Sundance 780 series
STATUS_UPDATE = 0xC4
LIGHTS_UPDATE = 0xCA
CC_REQ = 0xCC

#These are from a Jacuzzi 
CC_REQ_ALT_17 = 0x17
STATUS_UPDATE_ALT_16 = 0x16
LIGHTS_UPDATE_ALT_23 = 0x23

#Button CC equivs
BTN_CLEAR_RAY = 239
BTN_P1 = 228
BTN_P2 = 229
BTN_TEMP_DOWN = 226
BTN_TEMP_UP = 225
BTN_MENU =254
BTN_LIGHT_ON = 241
BTN_LIGHT_COLOR = 242
BTN_NA = 224

#Used to find our old channel, or an open channel
DETECT_CHANNEL_STATE_START = 0
DETECT_CHANNEL_STATE_CHANNEL_NOT_FOUND = 5 #Wait this man CTS cycles before deciding that a channel is available to use
NO_CHANGE_REQUESTED = -1 #Used to return control to other devices
CHECKS_BEFORE_RETRY = 2 #How many status messages we should receive before retrying our command



class SundanceRS485(BalboaSpaWifi):
     



    def __init__(self, hostname, port=8899):
        super().__init__(hostname, port)
               
        #debug
        logging.basicConfig()
        self.log = logging.getLogger(__name__)
        self.log.setLevel(logging.DEBUG)
        
        #Hard code some values that the base class needs and we dont know how to auto detect yet
        self.config_loaded = True
        self.pump_array = [1, 1, 1, 0, 0, 0]
        self.nr_of_pumps = 3
        self.circ_pump = 1
        self.tempscale = self.TSCALE_F #Can probably be determined...
        self.timescale = self.TIMESCALE_24H 
        self.temprange = 1
        
        self.filter_mode = 1 #Can probably be determined...
        self.heatmode = 0 #Can probably be determined...
        self.filter1_hour = 0 #Can probably be determined...
        self.filter1_duration_hours = 8 #Can probably be determined...
        self.filter2_enabled = 0  #Can probably be determined...
     
        #Setup some Model Specific Values
        self.day = -1
        self.month = -1
        self.year = -1
        self.temp2 = -1
        self.manualCirc = -1
        self.autoCirc = -1
        self.unknownCirc = -1
        self.heatState2 = -1      
        self.displayText = -1
        self.heatMode = -1
        self.UnknownField3 = -1
        self.UnknownField9 = -1
        self.panelLock = -1 #Assuming this can be determiend eventaully
        
        self.lightBrightnes = -1
        self.lightMode = -1
        self.lightR = -1
        self.lightG = -1
        self.lightB = -1
        self.lightCycleTime = -1
     
        #setup some sepcific items that we need that the base class doenst
        self.queue = queue.Queue() #Messages must e sent on CTS for our channel, not any time
        self.channel = None     #The channel we are assigned to
        self.discoveredChannels = [] #all the channels the tub is prodcign CTS's for
        self.activeChannels = [] #Channels we know are in use by other RS485 devices
        self.detectChannelState = DETECT_CHANNEL_STATE_START #STate machine used to find an open channel, or to get us a new one
        self.target_pump_status  = [NO_CHANGE_REQUESTED, NO_CHANGE_REQUESTED, NO_CHANGE_REQUESTED, NO_CHANGE_REQUESTED, NO_CHANGE_REQUESTED, NO_CHANGE_REQUESTED] #Not all messages seem to get accepted, so we have to check if our change compelted and retry if needed
        self.targetTemp = NO_CHANGE_REQUESTED
        
        self.targetlightMode = NO_CHANGE_REQUESTED
        self.targetlightBrightnes = NO_CHANGE_REQUESTED
        
        self.attemptsToCommand = 0
        
        self.checkCounter = 0
        self.checkCounterL = 0
        self.CAprior_status = None
        self.lastRGBMode = "White"
        self.lastBrightness = 100
        
        self.displayTextS = "unknown"    
        self.heatModeText ="unknown"  

        self.HEAT_MODE_MAP = [
            [32,"AUTO"],
            [34,"ECO"],
            [36,"DAY"],
        ]

        self.DISPLAY_MAP = [
            [22,"Set Temp"], #Observed when changing themperature, then went back to 23
            [23,"Current Temp"], #Previously Observed
            [36,"Current Temp"], #Previously Observed
            [32,"Current Temp"], 
            [31,"Current Temp"], #Observed while idle 
            [30,"Set Temp"],
            [35,"PF Set Primary Filtration"],
            [47,"SF Set Secondary Filtration"],
            [42,"HEAT Set Heat Mode"],
            [53,"FC Set Filter Change Interval"],
            [48,"UV Set Change Interval"],
            [51,"H2O Set Water Change Interval"],
            [62,"TIME Set Time"],
            [59,"DATE Set Date"],
            [0,"TEMP Set Temperature Units"],
            [3,"LANG Set Language"],
            [14,"LOCK Set Panel Lock"],
        ]

        self.LIGHT_MODE_MAP = [
            [128,"Fast Blend"], #with 2 second constant
            [127,"Slow Blend"], #wiht 4 secodn constant
            [255,"Frozen Blend"],
            [2,"BLue"],
            [7,"Violet"],
            [6,"Red"],
            [8,"Amber"],
            [3,"Green"],
            [9,"Aqua"],
            [1,"White"],
            [0,"Off"],
            [-1,"No Change"],
        ]
       

 
    async def connect(self):
        """ Connect to the spa."""
        try:
            self.reader, self.writer = await asyncio.open_connection(
                self.host, self.port
            )
        except (asyncio.TimeoutError, ConnectionRefusedError):
            self.log.error(
                "Cannot connect to spa at {0}:{1}".format(self.host, self.port)
            )
            return False
        except Exception as e:
            self.log.error(f"Error connecting to spa at {self.host}:{self.port}: {e}")
            return False
        self.connected = True
        sock = self.writer.transport.get_extra_info('socket')
        self.log.info(str(sock))
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return True

    async def send_temp_change(self, newtemp):
        """ Change the set temp to newtemp. """
        # Check if the new temperature is valid for the current heat mode
        if (
            newtemp < self.tmin[self.temprange][self.tempscale]
            or newtemp > self.tmax[self.temprange][self.tempscale]
        ):
            self.log.error("Attempt to set temperature outside of heat mode boundary")
            return
        self.attemptsToCommand = 0
        self.targetTemp = newtemp

    async def change_light(self, light, newstate):
        self.log.info("Not supported with New Format messaging")
        return 

    async def change_pump(self, pump, newstate):
        """ Change pump #pump to newstate. """
        # sanity check
        if (
            pump > MAX_PUMPS
            or newstate > self.pump_array[pump]
            or self.pump_status[pump] == newstate
        ):
            return
        self.attemptsToCommand = 0
        self.target_pump_status[pump] = newstate
        
    async def send_CCmessage(self, val):
        """ Sends a message to the spa with variable length bytes. """    
        # if not connected, we can't send a message
        if not self.connected:
            self.log.info("Tried to send CC message while not connected")
            return

        # if we dont have a channel number yet, we cant form a message
        if self.channel is None:
            self.log.info("Tried to send CC message without having been assigned a channel")
            return
            
        if self.attemptsToCommand > 64:
            self.log.info("Tried {} times to change state {} giving up.".format( self.attemptsToCommand, val))
            
        # Exampl: 7E 07 10 BF CC 65 85 A6 7E 
        message_length = 7
        data = bytearray(9)
        data[0] = M_STARTEND
        data[1] = message_length
        data[2] = self.channel
        data[3] = 0xBF
        data[4] = CC_REQ
        data[5] = val
        data[6] = 0
        data[7] = self.balboa_calc_cs(data[1:message_length], message_length - 1)
        data[8] = M_STARTEND

        self.log.debug(f"queueing message: {data.hex()}")
        self.queue.put(data)
        
        self.attemptsToCommand += 1

    async def send_message(self, *bytes):
        """ Sends a message to the spa with variable length bytes. """
        self.log.info("Not supported with New Format messaging")
        return 
        
    def xormsg(self, data):
        lst = []
        for i in range(0,len(data)-1,2):
                c = data[i]^data[i+1]^1
                lst.append(c)
        return lst

    async def parse_C4status_update(self, data):
        """Parse a status update from the spa.
        01 02 03 04 05 06 07 08 09 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29
        7E 26 FF AF C4 AE A7 AA AB A4 A1 C9 5D A5 A1 C2 A1 9C BD CE BB E2 B9 BB AD B4 B5 A7 B7 DF B1 B2 9B D3 8D 8E 8F 88 F9 7E
        """
        self.lastC4MessageReceived = time.time()
        
        #print ("".join(map("{:02X} ".format, bytes(data))))        
        #"Decrypt" / Decode the message
        data = self.xormsg(data[5:len(data)-2])



        #print ("x{}".format(data))

        """Parse a status update from the spa.
        01 02 03 04 05 06 07 08 09 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29
        [9, 0, 5, 148, 5, 99, 32, 124, 96, 23, 0, 33, 110, 39, 96, 0]
        """
 
        HOUR_FIELD = 0 #XOR 6 to get 24hour time
        PUMP_FIELD_1 = 1 #Most bit data
        DATE_FIELD_1 = 2 #Dont know how to use this yet...
        UNKOWN_FIELD_3 = 3 #Always 145? MIght might be days untill water refresh, UV, or filter change
        TBD_FIELD_4 = 4 #5 When Everything Off. 69 when clear ray / circ on?
        TEMP_FIELD_1 = 5 #Devide by 2 if in C, otherwise F
        HEAT_MODE_FIELD = 6 #
        DATE_FIELD_2 = 7
        SET_TEMP_FIELD = 8 #Devide by 2 if in C, otherwise F          
        UNKOWN_FIELD_9 = 9 #Always 107? might be days untill water refresh, UV, or filter change  
        HEATER_FIELD_1 = 10 #= 64 when Heat on        
        MINUTE_FIELD = 11 #Ok as is      
        UNKOWN_FIELD_12 = 12 #  Always 107? might be days untill water refresh, UV, or filter change           
        DISPLAY_FIELD = 13        
        TEMP_FEILD_2 = 14 #Appears to be 2nd temp sensor C  or F directly. Changes when pump is on!        

        PUMP_2_BIT_SHIFT = 2 #b100 When pump running
        PUMP_CIRC_BIT_SHIFT = 6 #b1000000 when pump running
        MANUAL_CIRC = 7 #b11000000 Includeding Pump Running
        AUTO_CIRC = 6 #b1100000 Includeding Pump Running
        TBD_4_CIRC_SHIFT = 6 #Field 4 goes up by 64 when circ is running it seems
        TEMP_FIELD_1_xor = 2 #need to Xor this field by 2 to get actual temperature for some reason
        HEATER_SHIFT_1 = 6 #b1000000 when Heat on
        DAY_SHIFT =  3 #Shift date field 2 by this amount to get day of month
        MONTH_AND = 7  #Shift date field 2 by this to get Month of year
        #YEAR Dont have a guess yet
        HOUR_XOR = 6 #Need to xor the hour field with 6 to get actual hour
        

        #High Confidance
        time_hour = data[HOUR_FIELD]^HOUR_XOR
        time_minute = data[MINUTE_FIELD]    
        pump0 = (data[DATE_FIELD_1] >> 4) & 1           
        pump1 = (data[PUMP_FIELD_1] >> PUMP_2_BIT_SHIFT) & 1     
        circ_pump_status = (data[PUMP_FIELD_1]>> PUMP_CIRC_BIT_SHIFT) & 1
        autoCirc = (data[PUMP_FIELD_1] >> AUTO_CIRC) & 1  
        manualCirc = (data[PUMP_FIELD_1] >> MANUAL_CIRC) & 1       
        settemp = float(data[SET_TEMP_FIELD])
        settemp = settemp / (2 if self.tempscale == self.TSCALE_C else 1)               
        temp = data[TEMP_FIELD_1]^TEMP_FIELD_1_xor
        curtemp = (
            temp / (2 if self.tempscale == self.TSCALE_C else 1)
            if temp != 255
            else None
        )
        
        heatstate = (data[HEATER_FIELD_1] >> HEATER_SHIFT_1) & 1
        displayText = data[DISPLAY_FIELD]        
        heatMode = data[HEAT_MODE_FIELD]
        day = (data[DATE_FIELD_2] >> DAY_SHIFT) 
        month = (data[DATE_FIELD_2] & MONTH_AND) 
      
 
        displayTextS = "unknown"
        for x,y in self.DISPLAY_MAP:
            if x == displayText:
               displayTextS = y 
               break
        
        heatModeText = "unknown"
        for x,y in self.HEAT_MODE_MAP:
            if x == heatMode:
               heatModeText = y 
               break
        

 
        #Medium Confidance
        unknownCirc = (data[TBD_FIELD_4] >> TBD_4_CIRC_SHIFT) & 1
        temp2 = float(data[TEMP_FEILD_2])
        if(self.circ_pump_status == 1): #Unclear why this is ncessary
            temp2 = temp2 + 32   
        temp2 = temp2 #Hide the data here for now   
            
        #Low Confidance
        UnknownField3 = data[UNKOWN_FIELD_3]
        UnknownField9 = data[UNKOWN_FIELD_9]       
        UnknownField12 = data[UNKOWN_FIELD_12]     


   
   
        #FIND OUT IF OUR LAST COMMAND WORKED...
        if self.checkCounter > 0:
            self.checkCounter -= 1
            
        if (self.checkCounter == 0):
            if(self.settemp  != self.targetTemp and self.targetTemp != NO_CHANGE_REQUESTED):
                if self.targetTemp < self.settemp:
                    await self.send_CCmessage(226) #Temp Down Key
                else:
                    await self.send_CCmessage(225) #Temp Up Key
                self.checkCounter = CHECKS_BEFORE_RETRY
            elif self.settemp  == self.targetTemp:
                self.targetTemp = NO_CHANGE_REQUESTED
                
        if (self.checkCounter == 0):
            for i in range(0,len(self.target_pump_status)):
                if self.pump_status[i] != self.target_pump_status[i] and self.target_pump_status[i] != NO_CHANGE_REQUESTED:
                    if i == 0:
                        await self.send_CCmessage(228) #Pump 1 Button
                    elif i == 1: 
                        await self.send_CCmessage(229) #Pump 2 Button
                    else:
                        await self.send_CCmessage(239) #Clear Ray / Circulating Pump
                    self.checkCounter = CHECKS_BEFORE_RETRY
                elif self.pump_status[i] == self.target_pump_status[i]:
                    self.target_pump_status[i] = NO_CHANGE_REQUESTED



        
        displayNewData = False
        unknownChange = True
        if self.time_hour != time_hour:
            unknownChange = False
        if self.time_minute != time_minute:
            unknownChange = False
        if self.pump_status[0] != pump0:
            displayNewData = True
            self.log.debug("pump0 {} {}".format( self.pump_status[0], pump0))
        if self.pump_status[1] != pump1:
            displayNewData = True
            self.log.debug("pump1 {} {}".format( self.pump_status[1], pump1))
        if self.circ_pump_status != circ_pump_status:
            displayNewData = True
            self.log.debug("circ_pump_status {} {}".format( self.circ_pump_status, circ_pump_status))
        if self.settemp != settemp:
            displayNewData = True
            self.log.debug("settemp {} {}".format( self.settemp, settemp))
        if self.curtemp != curtemp:
            displayNewData = True
            self.log.debug("curtemp {} {}".format( self.curtemp, curtemp))
        if self.heatstate != heatstate:
            displayNewData = True
            self.log.debug("heatstate {} {}".format( self.heatstate, heatstate))
        if self.displayText != displayText:
            displayNewData = True
            self.log.debug("displayText {} {} {} {}".format( self.displayText, self.displayTextS, displayText,  displayTextS))
        if self.heatMode != heatMode:
            displayNewData = True
            self.log.debug("heatMode {} {} {} {}".format( self.heatMode, self.heatModeText, heatMode,  heatModeText))
        if self.month != month:
            displayNewData = True
            self.log.debug("month {} {}".format( self.month, month))
        if self.day != day:
            displayNewData = True
            self.log.debug("day {} {}".format( self.day, day))
        if self.autoCirc != autoCirc:
            displayNewData = True
            self.log.debug("autoCirc {} {}".format( self.autoCirc, autoCirc))
        if self.manualCirc != manualCirc:
            displayNewData = True
            self.log.debug("manualCirc {} {}".format( self.manualCirc, manualCirc))
        if self.temp2 != temp2:
            displayNewData = True
            self.log.debug("temp2 Old {}  New {} Temp1 {}".format( self.temp2, temp2, curtemp))

        self.time_hour = time_hour
        self.time_minute = time_minute
        self.pump_status[0] = pump0
        self.pump_status[1] = pump1
        self.circ_pump_status = circ_pump_status
        self.pump_status[2] = circ_pump_status
        self.settemp = settemp
        self.curtemp = curtemp
        self.heatstate = heatstate
        self.displayText = displayText
        self.heatMode = heatMode
        self.month = month
        self.day = day
        self.autoCirc = autoCirc
        self.manualCirc = manualCirc       
            
        self.displayTextS = displayTextS    
        self.heatModeText = heatModeText      


        #Medium Confidance
        self.unknownCirc = unknownCirc
        self.temp2 = temp2  
            
        #Low Confidance
        self.UnknownField3 = UnknownField3
        self.UnknownField9 = UnknownField3           
        self.UnknownField12  =  UnknownField12


        # Check if any bytes changed
        have_new_data = False
        if self.prior_status is not None:
            for i in range(0, len(data)):
                if data[i] != self.prior_status[i]:
                    have_new_data = True
                    break
        else:
            have_new_data = True
            self.prior_status = bytearray(len(data))
              
        if not have_new_data:
             return
             
        self.lastupd = time.time()


        if displayNewData:
            unknownChange = False
        if unknownChange or displayNewData:
            self.log.info("Time: {}".format(datetime.fromtimestamp(self.lastupd).strftime("%Y-%m-%d %H:%M:%S") ))
            self.log.info("Unknown Change: {}        Full Message: C4: {}".format(unknownChange, data))
            for i in range(0, len(data)):
                if(self.prior_status[i] != data[i]):
                    self.log.info("Changed Field: {} Old: {} New: {}".format(i, self.prior_status[i], data[i]))
            self.log.info("{}-{} {}:{} P0:{} P1:{} Circ:{}  settemp:{}  curtemp:{}  heatstate:{}  displayText:{} {}  heatMode:{} {}  ".format(month,day, time_hour,time_minute,pump0,pump1,circ_pump_status,settemp,curtemp,heatstate,displayText, self.displayTextS, heatMode, self.heatModeText))
            self.log.info("unknownCirc:{} temp2:{} UnknownField3:{}  UnknownField9:{} UnknownField12:{} ".format(unknownCirc, temp2,UnknownField3,UnknownField9, UnknownField12))
 
        # Check if any bytes changed
        have_new_data = False
        if self.prior_status is not None:
            for i in range(0, len(data)):
                if data[i] != self.prior_status[i]:
                    have_new_data = True
                    break
        else:
            have_new_data = True
            self.prior_status = bytearray(len(data))
              
        if not have_new_data:
             return
             
        self.lastupd = time.time()
 
         # populate prior_status
        for i in range(0, len(data)):
            self.prior_status[i] = data[i]

        await self.int_new_data_cb()
                     

        

    async def parse_CA_light_status_update(self, data):
        """Parse a status update from the spa.
        01 02 03 04 05 06 07 08 09 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29
        7E 22 FF AF CA 8A 36 CA CB C4 C5 C6 FB C0 C1 C2 3C DC DD DE DF D8 D9 DA DB D4 D5 D6 D7 D0 D1 D2 D3 EC E5 7E 
        """
        #"Decrypt" the message
        data = self.xormsg(data[5:len(data)-2])
        
        """Parse a status update from the spa.
        01 02 03 04 05 06 07 08 09 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29
        TODO Example after decryption
        """      
        #TODO: The rest...
        
        LIGHT_MODE_FIELD = 0 #TBD
     
        self.lightBrightnes = data[1]
        self.lightMode = data[4]
        self.lightB = data[2]
        self.lightG = data[6]
        self.lightR = data[8]    
        self.lightCycleTime= data[9] 
        self.lightUnknown1 = data[0]
        self.lightUnknown3 = data[3]
        self.lightUnknown4 = data[5]
        self.lightUnknown7 = data[7]
        self.lightUnknown9 = data[9]
        
        self.lightModeText = "unknown"
        for x,y in self.LIGHT_MODE_MAP:
            if x == self.lightMode:
               self.lightModeText = y 
               break   
               
        if self.checkCounterL > 0:
            self.checkCounterL -= 1
            
        if (self.checkCounterL == 0):
            if(self.targetlightMode  != self.lightMode and self.targetlightMode != NO_CHANGE_REQUESTED):
                await self.send_CCmessage(BTN_LIGHT_COLOR) 
                self.checkCounterL = CHECKS_BEFORE_RETRY
            elif self.targetlightMode  == self.lightMode:
                self.targetlightMode = NO_CHANGE_REQUESTED

        if (self.checkCounterL == 0):
            if(self.targetlightBrightnes  != self.lightBrightnes and self.targetlightBrightnes != NO_CHANGE_REQUESTED):
                await self.send_CCmessage(BTN_LIGHT_ON) 
                self.checkCounterL = CHECKS_BEFORE_RETRY
            elif self.targetlightBrightnes  == self.lightBrightnes:
                self.targetlightBrightnes = NO_CHANGE_REQUESTED                


        have_new_data = False
        if self.CAprior_status is not None:
            for i in range(0, len(data)):
                if data[i] != self.CAprior_status[i]:
                    have_new_data = True
                    break
        else:
            have_new_data = True
            self.CAprior_status = bytearray(len(data))

        
        if not have_new_data:
             return
             
        self.log.info("CA{}".format(data))
        self.lastupd = time.time()
        for i in range(0, len(data)):
            self.CAprior_status[i] = data[i]
        
        
    async def setMyChan(self, chan):
        self.channel = chan
        self.log.info("Got assigned channel = {}".format(self.channel))
        message_length = 7
        self.NTS = bytearray(9)
        self.NTS[0] = M_STARTEND
        self.NTS[1] = message_length
        self.NTS[2] = self.channel
        self.NTS[3] = 0xBF
        self.NTS[4] = CC_REQ
        self.NTS[5] = 0 #Dummy
        self.NTS[6] = 0
        self.NTS[7] = self.balboa_calc_cs(self.NTS[1:message_length], message_length - 1)
        self.NTS[8] = M_STARTEND

    async def listen(self):
        """ Listen to the spa babble forever. """
        
        #teststring = "7E25FFAF161012270B16420026FA260A140181000042011C00098000000A000000FF0000002F7E"
        #data = bytes.fromhex(teststring)
        #await self.parse_C4status_update(data)
        
        #return
        
        while True:
            if not self.connected:
                # sleep and hope the checker fixes us
                await asyncio.sleep(5)
                continue

            data = await self.read_one_message()
            if data is None:
                #await asyncio.sleep(0.0001)
                continue

            channel = data[2]
            mid = data[3]
            mtype = data[4]

            #print("a")


            if mtype == STATUS_UPDATE:
                await self.parse_C4status_update(data)
            elif mtype == LIGHTS_UPDATE:
                await self.parse_CA_light_status_update(data)
            elif mtype == STATUS_UPDATE_ALT_16:
                await self.parse_C4status_update(data)
            elif mtype == LIGHTS_UPDATE_ALT_23:
                await self.parse_CA_light_status_update(data)
            elif mtype == CLIENT_CLEAR_TO_SEND:
                if self.channel is None and self.detectChannelState == DETECT_CHANNEL_STATE_CHANNEL_NOT_FOUND:
                    message_length = 8
                    data = bytearray(10)
                    data[0] = M_STARTEND
                    data[1] = message_length
                    data[2] = 0xFE
                    data[3] = 0xBF
                    data[4] = CHANNEL_ASSIGNMENT_REQ #type
                    data[5] = 0x02
                    data[6] = 0xF1 #random Magic
                    data[7] = 0x73
                    data[8] = self.balboa_calc_cs(data[1:message_length], message_length - 1)
                    data[9] = M_STARTEND
                    self.writer.write(data)
                    await self.writer.drain()                        
            elif mtype == CHANNEL_ASSIGNMENT_RESPONCE:
                #TODO check for magic numbers to be repeated back
                await self.setMyChan(data[5])
                message_length = 5
                data = bytearray(7)
                data[0] = M_STARTEND
                data[1] = message_length
                data[2] = self.channel
                data[3] = 0xBF
                data[4] = CHANNEL_ASSIGNMENT_ACK #type
                data[5] = self.balboa_calc_cs(data[1:message_length], message_length - 1)
                data[6] = M_STARTEND
                self.writer.write(data) 
                await self.writer.drain()                   
            elif mtype == EXISTING_CLIENT_REQ:                      
                message_length = 8
                data = bytearray(9)
                data[0] = M_STARTEND
                data[1] = message_length
                data[2] = self.channel
                data[3] = 0xBF
                data[4] = EXISTING_CLIENT_RESPONCE #type
                data[5] = 0x04 #Dont know!
                data[6] = 0x08 #Dont know!
                data[7] = 0x00 #Dont know!
                data[8] = self.balboa_calc_cs(data[1:message_length], message_length - 1)
                data[9] = M_STARTEND
                self.writer.write(data)
                await self.writer.drain()
            elif mtype == CLEAR_TO_SEND:               
                if not channel in  self.discoveredChannels:
                    self.discoveredChannels.append(data[2])
                    #print("Discovered Channels:" + str(self.discoveredChannels))
                    #detec conflict
                    if data[2] == self.channel:
                        self.log.warn("Found a channel conflict, getting a new channel")
                        self.channel = None
                        self.detectChannelState = DETECT_CHANNEL_STATE_START
                elif channel == self.channel:
                    if self.queue.empty():
                        #self.writer.write(self.NTS)
                        await self.writer.drain()
                    else:
                        msg = self.queue.get()
                        self.writer.write(msg)
                        await self.writer.drain()
                        self.log.debug("sent")
            else:
                if (mtype == CC_REQ) or  (mtype == CC_REQ_ALT_17):
                    if not channel in  self.activeChannels:
                        self.activeChannels.append(data[2])
                        self.log.info("Active Channels:" + str(self.activeChannels))
                    elif  self.detectChannelState < DETECT_CHANNEL_STATE_CHANNEL_NOT_FOUND:
                        self.detectChannelState += 1
                        if self.detectChannelState == DETECT_CHANNEL_STATE_CHANNEL_NOT_FOUND:
                            self.discoveredChannels.sort()
                            for chan in self.discoveredChannels:
                                if not chan in self.activeChannels:
                                    await self.setMyChan( chan)
                                    break
                    if (mtype == CC_REQ_ALT_17):
                        if (data[5]) != 0:
                            self.log.info("Got Button Press x".format(channel, mid, mtype) + "".join(map("{:02X} ".format, bytes(data))))
                    if (mtype == CC_REQ):
                        buttondata = data[5]^data[6]
                        if buttondata != 224:
                            self.log.info("Got Button Press {} {} : ".format(data[5]^data[6]^1, buttondata) + "".join(map("{:02X} ".format, bytes(data))))
                elif (mtype > NOTHING_TO_SEND) :
                    self.log.warn("Unknown Message {:02X} {:02X} {:02X} x".format(channel, mid, mtype) + "".join(map("{:02X} ".format, bytes(data))))
              
    async def spa_configured(self):
            return True
        
    async def listen_until_configured(self, maxiter=20):
        """ Listen to the spa babble until we are configured."""
        return True
      
   
   
   
    def get_day(self):
        return self.day
        
    def get_month(self):    
        return self.month 
        
    def get_year(self):  
        return self.year 
        
    def get_temp2(self):  
        return self.temp2 
        
    def get_manualCirc(self):  
        return self.manualCirc 
        
    def get_autoCirc(self):  
        return self.autoCirc
        
    def get_unknownCirc(self):  
        return self.unknownCirc
        
    def get_heatState2(self):  
        return self.heatState2

    def get_displayText(self):  
        return self.displayText 
        
    def get_heatMode(self):  
        return self.heatMode 
        
    def get_UnknownField3(self):  
        return self.UnknownField3
        
    def get_UnknownField9(self):  
        return self.UnknownField9 
        
    def get_panelLock(self):  
        return self.panelLock 
        
    def get_LightBrightnes(self):  
        return self.LightBrightnes
        
    def get_lightMode(self):  
        return self.lightMode
        
    def get_lightR(self):  
        return self.lightR
        
    def get_lightG(self):  
        return self.lightG
        
    def get_lightB(self):  
        return self.lightB 
         
    async def change_rgbbrightness(self, light, newstate):
        if newstate  < 16.5:
            newstate = 0
        elif newstate < 49.5:
            newstate = 33
        elif newstate < 82.25:
            newstate = 66
        else:
            newstate = 100
        self.attemptsToCommand = 0          
        self.targetlightBrightnes = newstate
   
    async def change_rgbmode(self, light, newstate):
        #cant change mode when off
        if self.lightBrightnes == 0:
            return            
        self.attemptsToCommand = 0     
        newmode = -1
        for x,y in self.LIGHT_MODE_MAP:
            if y == newstate:
                newmode = x 
        self.targetlightMode = newmode
