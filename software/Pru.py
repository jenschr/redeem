'''
Pru.py file for Replicape. 

Author: Elias Bakken
email: elias(dot)bakken(at)gmail(dot)com
Website: http://www.thing-printer.com
License: CC BY-SA: http://creativecommons.org/licenses/by-sa/2.0/
'''
PRU0_ARM_INTERRUPT     = 19
PRU1_ARM_INTERRUPT     = 20
ARM_PRU0_INTERRUPT     = 21
ARM_PRU1_INTERRUPT     = 22

PRU0                   = 0
PRU1                   = 1

import os
import logging
import pypruss      					                            # The Programmable Realtime Unit Library
import numpy as np						                            # Needed for braiding the pins with the delays
from threading import Thread, Lock
import Queue
import time 
import mmap
import struct 
import select

from collections import deque

DDR_MAGIC			= 0xbabe7175

class Pru:
    ddr_lock = Lock()

    def __init__(self):
        pru_hz 			    = 200*1000*1000             # The PRU has a speed of 200 MHz
        self.s_pr_inst      = 2.0*(1.0/pru_hz)          # I take it every instruction is a single cycle instruction
        self.inst_pr_loop 	= 0                        # This is the minimum number of instructions needed to step.  It is already substracted into the PRU
        self.inst_pr_delay 	= 2                         # Every loop adds two instructions: i-- and i != 0            
        self.sec_to_inst_dev = (self.s_pr_inst*2)
        self.pru_data       = []      	    	        # This holds all data for one move (x,y,z,e1,e2)
        self.ddr_used       = Queue.Queue()             # List of data lengths currently in DDR for execution
        self.ddr_reserved   = 0      
        self.ddr_mem_used   = 0  
        self.clear_events   = []       

        self.ddr_addr = int(open("/sys/class/uio/uio0/maps/map1/addr","rb").read().rstrip(), 0)
        self.ddr_size = int(open("/sys/class/uio/uio0/maps/map1/size","rb").read().rstrip(), 0)
        logging.info("The DDR memory reserved for the PRU is "+hex(self.ddr_size)+" and has addr "+hex(self.ddr_addr))

        ddr_offset     		= self.ddr_addr-0x20000000  # The Python mmap function cannot accept unsigned longs. 
        ddr_filelen    		= self.ddr_size+0x20000000
        self.DDR_START      = 0x20000000
        self.DDR_END        = 0x20000000+self.ddr_size
        self.ddr_start      = self.DDR_START
        self.ddr_nr_events  = self.ddr_addr+self.ddr_size-4
        self.interrupted    = False

        with open("/dev/mem", "r+b") as f:	            # Open the memory device
            self.ddr_mem = mmap.mmap(f.fileno(), ddr_filelen, offset=ddr_offset) # mmap the right area            
            self.ddr_mem[self.ddr_start:self.ddr_start+4] = struct.pack('L', 0)  # Add a zero to the first reg to make it wait
       
        dirname = os.path.dirname(os.path.realpath(__file__))
        pypruss.init()						            # Init the PRU
        pypruss.open(PRU0)						        # Open PRU event 0 which is PRU0_ARM_INTERRUPT
        pypruss.pruintc_init()					        # Init the interrupt controller
        pypruss.pru_write_memory(0, 0, [self.ddr_addr, self.ddr_nr_events, 0])		# Put the ddr address in the first region 
        pypruss.exec_program(0, dirname+"/../firmware/firmware_00A3.bin")	# Load firmware "ddr_write.bin" on PRU 0
        
        #Wait until we get the GPIO output in the DDR
        self.dev = os.open("/dev/uio0", os.O_RDONLY)

        ret = select.select( [self.dev],[],[], 1.0 )
        if ret[0] == [self.dev]:
            pypruss.clear_event(PRU0_ARM_INTERRUPT)         # Clear the event        
        
        self.initial_gpio = [struct.unpack("L", self.ddr_mem[self.DDR_START+4:self.DDR_START+8])[0], struct.unpack("L", self.ddr_mem[self.DDR_START+8:self.DDR_START+12])[0], struct.unpack("L", self.ddr_mem[self.DDR_START+12:self.DDR_START+16])[0], struct.unpack("L", self.ddr_mem[self.DDR_START+16:self.DDR_START+20])[0] ]

        os.close(self.dev)

        #Clear DDR
        self.ddr_mem[self.DDR_START+4:self.DDR_START+8] = struct.pack('L', 0)
        self.ddr_mem[self.DDR_START+8:self.DDR_START+12] = struct.pack('L', 0)
        self.ddr_mem[self.DDR_START+12:self.DDR_START+16] = struct.pack('L', 0)
        self.ddr_mem[self.DDR_START+16:self.DDR_START+20] = struct.pack('L', 0)

        self.t = Thread(target=self._wait_for_events)         # Make the thread
        self.t.daemon = True
        self.running = True
        self.t.start()		

    def read_gpio_state(self, gpio_bank):
        """ Return the initial state of a GPIO bank when the PRU was initialized """
        return self.initial_gpio[gpio_bank]
    
    def add_data(self, data):
        """ Add some data to one of the PRUs """
        (pins, dirs, delays) = data                       	    # Get the data
        delays = np.clip(0.5*((np.array(delays)/self.s_pr_inst)-self.inst_pr_loop), 1, 4294967296L)
        data = np.array([pins,dirs, delays.astype(int)])		        	    # Make a 2D matrix combining the ticks and delays
        #data = list(.flatten())     	    # Braid the data so every other item is a pin and delay
        self.pru_data = data.transpose()   

    def has_capacity_for(self, data_len):
        """ Check if the PRU has capacity for a chunk of data """
        return (self.get_capacity() > data_len)
    
    def get_capacity(self):
        """ Check if the PRU has capacity for a chunk of data """
        return self.ddr_size-self.ddr_mem_used

    def is_empty(self):
        """ If no PRU data is processing, return true """
        self.ddr_used.empty()

    def wait_until_done(self):
        """ Wait until the queue is empty """
        self.ddr_used.join()
    
    def is_processing(self):
        """ Returns True if there are segments on queue """
        return not self.is_empty()

    def interrupt_move(self):
        """ Interrupt the current movements and all the ones which are stored in DDR """
        self.ddr_mem[self.DDR_START:self.DDR_START+4] = struct.pack('L', 0)
        self.interrupted = True
        pypruss.pru_write_memory(0, 0, [self.ddr_addr, self.ddr_nr_events, 1])

    def pack(self, word):
        return struct.pack('L', word)

    ''' Commit the data to the DDR memory '''
    def commit_data(self):
        
        data = struct.pack('L', len(self.pru_data))	    	# Pack the number of toggles. 
        #Then we have one byte, one byte, one 16 bit (dummy), and one 32 bits
        print "PRU"
        print self.pru_data
        data += ''.join([struct.pack('BBHL', instr[0],instr[1],0,instr[2]) for instr in self.pru_data])
        data += struct.pack('L', 0)                             # Add a terminating 0, this keeps the fw waiting for a new command.
        print ":".join("{0:x}".format(ord(c)) for c in data)
        self.ddr_end = self.ddr_start+len(data)       
        if self.ddr_end >= self.DDR_END-16:                     # If the data is too long, wrap it around to the start
            multiple = (self.DDR_END-16-self.ddr_start)%2       # Find a multiple of 8: 4*(pins, delays)
            cut = self.DDR_END-16-self.ddr_start-multiple-4     # The cut must be done after a delay, so a multiple of 8 bytes +/-4
            
            if cut == 4: 
                logging.error("Cut was 4, setting it to 12")
                cut = 12                
            logging.debug("Data len is "+hex(len(data))+", Cutting the data at "+hex(cut))

            first = struct.pack('L', len(data[4:cut])/2)+data[4:cut]    # Update the loop count
            first += struct.pack('L', DDR_MAGIC)                        # Add the magic number to force a reset of DDR memory counter
            #logging.warning("First batch starts from "+hex(self.ddr_start)+" to "+hex(self.ddr_start+len(first)))
            self.ddr_mem[self.ddr_start:self.ddr_start+len(first)] = first  # Write the first part of the data to the DDR memory.

            with Pru.ddr_lock: 
                self.ddr_mem_used += len(first)
            self.ddr_used.put(len(first))

            if len(data[cut:-4]) > 0:                                 # If len(data) == 4, only the terminating zero is present..
                second = struct.pack('L', (len(data[cut:-4])/8))+data[cut:]     # Add the number of steps in this iteration
                self.ddr_end = self.DDR_START+len(second)           # Update the end counter
                #logging.warning("Second batch starts from "+hex(self.DDR_START)+" to "+hex(self.ddr_end))
                self.ddr_mem[self.DDR_START:self.ddr_end] = second  # Write the second half of data to the DDR memory.
                with Pru.ddr_lock: 
                    self.ddr_mem_used += len(second)
                self.ddr_used.put(len(second))

            else:
                self.ddr_end = self.DDR_START+4
                self.ddr_mem[self.DDR_START:self.ddr_end] = struct.pack('L', 0) # Terminate the first word
                logging.debug("Second batch skipped, 0 length")
            #logging.warning("")
        else:

            self.ddr_mem[self.ddr_start:self.ddr_end] = data    # Write the data to the DDR memory.
            data_len = len(data)
            with Pru.ddr_lock: 
                self.ddr_mem_used += data_len               
            self.ddr_used.put(data_len)                         # update the amount of memory used 
            logging.debug("Pushed "+str(data_len)+" from "+hex(self.ddr_start)+" to "+hex(self.ddr_end))
            
        self.ddr_start  = self.ddr_end-4    # Update the start of ddr for next time 
        self.pru_data   = []                # Reset the pru_data list since it has been commited         

    def _clear_after_interrupt(self):
        with Pru.ddr_lock: 
            self.pru_data       = []                        # This holds all data for one move (x,y,z,e1,e2)
            self.ddr_reserved   = 0      
            self.ddr_mem_used   = 0  
            self.clear_events   = []       
            self.ddr_start      = self.DDR_START
            self.ddr_mem[self.ddr_start:self.ddr_start+4] = struct.pack('L', 0)  # Add a zero to the first reg to make it wait
            while True:
                try:
                    v = self.ddr_used.get(block=False)
                    if v != None:
                        self.ddr_used.task_done()
                except Queue.Empty:
                    break

        self.interrupted = False
        pypruss.pru_write_memory(0, 0, [self.ddr_addr, self.ddr_nr_events, 0])

    ''' Catch events coming from the PRU '''                
    def _wait_for_events(self):
        events_caught = 0
        self.dev = os.open("/dev/uio0", os.O_RDONLY)
        self.new_events = 0
        self.old_events = 0
        nr_interrupts = 0
        while self.running:
            ret = select.select( [self.dev],[],[], 1.0 )
            if ret[0] == [self.dev]:
                self._wait_for_event()
                pypruss.clear_event(PRU0_ARM_INTERRUPT)			# Clear the event        
                nr_events = struct.unpack("L", self.ddr_mem[self.DDR_END-4:self.DDR_END])[0]   
            else:
                nr_events = struct.unpack("L", self.ddr_mem[self.DDR_END-4:self.DDR_END])[0]

            if self.interrupted:
                self._clear_after_interrupt()
                continue

            while nr_interrupts < nr_events:
                ddr = self.ddr_used.get()                       # Pop the first ddr memory amount           
                with Pru.ddr_lock: 
                    self.ddr_mem_used -= ddr                    
                logging.debug("Popped "+str(ddr)+"\tnow "+hex(self.get_capacity()))
                if self.get_capacity() < 0:
                    logging.error("Capacity less than 0!")
                if self.get_capacity() == 0x40000:
                    logging.warning("PRU empty!")                    
                nr_interrupts += 1  
                self.ddr_used.task_done()
                                   

    ''' Wait for an event. The return is the number of events that have occured since last check '''
    def _wait_for_event(self):
        self.new_events =  struct.unpack("L", os.read(self.dev, 4))[0]
        ret = self.new_events-self.old_events
        self.old_events = self.new_events
        return ret

    def force_exit(self):
        self.running = False  
        pypruss.pru_disable(0)                                  # Disable PRU 0, this is already done by the firmware
        pypruss.exit()                                          # Exit, don't know what this does. 

    ''' Close shit up '''
    def join(self):
        logging.debug("joining")
        self.running = False
        self.t.join()        
        self.ddr_mem.close()                                    # Close the memory        
        pypruss.pru_disable(0)                                  # Disable PRU 0, this is already done by the firmware
        pypruss.exit()                                          # Exit, don't know what this does. 
        
