import wlmData
import wlmConst
import ctypes
# others
import sys



class wlm_link():
    def __init__(self):
        #########################################################
        # Set the DLL_PATH variable according to your environment
        #########################################################
        DLL_PATH = "wlmData.dll"
        self.verbose = False
        # Load DLL from DLL_PATH
        try:
            wlmData.LoadDLL(DLL_PATH)
        except:
            sys.exit("Error: Couldn't find DLL on path %s. Please check the DLL_PATH variable!" % DLL_PATH)


        if wlmData.dll.GetWLMCount(0) == 0:
            print("There is no running wlmServer instance(s).")
        else:
            # Read Type, Version, Revision and Build number
            self.Version_type = wlmData.dll.GetWLMVersion(0)
            self.Version_ver = wlmData.dll.GetWLMVersion(1)
            self.Version_rev = wlmData.dll.GetWLMVersion(2)
            self.Version_build = wlmData.dll.GetWLMVersion(3)
            print("WLM Version: [%s.%s.%s.%s]" % (self.Version_type, self.Version_ver, self.Version_rev, self.Version_build))
            #wlmData.dll.Operation(wlmConst.cCtrlStartMeasurement)


    #Swtiching mode
    #TODO: all channels switch enable
    #each channel get/set switch mode
    def get_switcher_signal(self, port):
        use_state = ctypes.c_long()
        show_state = ctypes.c_long()

        wlmData.dll.GetSwitcherSignalStates(ctypes.c_long(port),
                                             ctypes.byref(use_state), ctypes.byref(show_state))
        return use_state.value, show_state.value


    def set_switcher_signal(self, port, use, show):
        return wlmData.dll.SetSwitcherSignal(port, use, show)


    #Status
    def get_temperature(self):
        temperature = wlmData.dll.GetTemperature(0.0)
        return temperature
    
    def get_pressure(self):
        pressure = wlmData.dll.GetPressure(0.0)
        return pressure
    

    #Autocalibration
    def get_autocal_mode(self):
        autocal_mode = wlmData.dll.GetAutoCalMode(0)
        return autocal_mode
    
    def set_autocal_mode(self, status):
        return wlmData.dll.SetAutoCalMode(status)

    

    #TODO: GetAutoCalSetting


    ##Basic comms
    def get_frequency(self):
        Frequency = wlmData.dll.GetFrequency(0.0)
        StatusString = ""
        if Frequency == wlmConst.ErrWlmMissing:
            StatusString = "WLM inactive"
        elif Frequency == wlmConst.ErrNoSignal:
            StatusString = 'No Signal'
        elif Frequency == wlmConst.ErrBadSignal:
            StatusString = 'Bad Signal'
        elif Frequency == wlmConst.ErrLowSignal:
            StatusString = 'Low Signal'
        elif Frequency == wlmConst.ErrBigSignal:
            StatusString = 'High Signal'
        else:
            StatusString = 'WLM is running'
        if self.verbose:
            print("status: "+StatusString) # TODO: consider passing back the status
        return Frequency


    def get_frequency_num(self, port):
        Frequency = wlmData.dll.GetFrequencyNum(port, 0.0)
        StatusString = ""
        if Frequency == wlmConst.ErrWlmMissing:
            StatusString = "WLM inactive"
        elif Frequency == wlmConst.ErrNoSignal:
            StatusString = 'No Signal'
        elif Frequency == wlmConst.ErrBadSignal:
            StatusString = 'Bad Signal'
        elif Frequency == wlmConst.ErrLowSignal:
            StatusString = 'Low Signal'
        elif Frequency == wlmConst.ErrBigSignal:
            StatusString = 'High Signal'
        else:
            StatusString = 'WLM is running'
        if self.verbose:
            print("Port %i status: "%port + StatusString) # TODO: consider passing back the status
        return Frequency
    
    def is_active(self):
        """Return True if at least one wlmServer instance is running."""
        try:
            return wlmData.dll.GetWLMCount(0) > 0
        except Exception:
            return False

    def get_exposure_num(self, port):
        Exposure_1 = wlmData.dll.GetExposureNum(int(port),1, 0)
        Exposure_2 = wlmData.dll.GetExposureNum(int(port),2, 0)
        if Exposure_1 == wlmConst.ErrWlmMissing:
            if self.verbose:
                print("Exposure: WLM not active")
        elif Exposure_1 == wlmConst.ErrNotAvailable:
            if self.verbose:
                print("Exposure: not available")
        else:
            if self.verbose:
                print("Exposure: %d ms" % Exposure_1)
        
        return Exposure_1, Exposure_2

    def get_amplitude(self, port):
        #TODO:make a num function and a non num function for this!
        """
        returns the max amplitude of the two ccd arrays
        """
        amp1 = wlmData.dll.GetAmplitudeNum(port,wlmConst.cMax1,0)
        amp2 = wlmData.dll.GetAmplitudeNum(port,wlmConst.cMax2,0)

        return amp1, amp2
    #TODO:
    #Other functinos that would be helpful to have:
    #Averaging control

    def get_deviation_mode(self):
        return wlmData.dll.GetDeviationMode(0)
    
    def set_deviation_mode(self, status):
        return wlmData.dll.SetDeviationMode(status)

    ##PID related functions
    def get_pid_settings(self):
        #not sure what this exactly does yet
        intval=ctypes.c_long(0)
        doubleval=ctypes.c_double(0)
        wlmData.dll.GetPIDSetting(wlmConst.cmiPID_P,1,intval,doubleval)#intval and doubleval will have the values
    
    # def get_pid_course_num(self, port):
    #     string=(ctypes.c_char*1024)()
    #     wlmData.dll.GetPIDCourseNum(port,ctypes.cast(string, ctypes.POINTER(ctypes.c_char)))
    #     float_value = float(string.value.decode().replace(',', '.'))
    #     print("Target wavelength port %i = %.9f"%(port, float_value))
    #     return float_value

    def get_pid_course_num(self, port):
        """Retrieves the setpoint wavelength for a given port.
        On 14-03-2025 suddenly the wavemeter started sending '= xxx.xxxx' format, so added a strip function here to remove that.
        """
        string = (ctypes.c_char * 1024)()  # Create a buffer to hold the response
        wlmData.dll.GetPIDCourseNum(port, ctypes.cast(string, ctypes.POINTER(ctypes.c_char)))
        
        # Decode and clean the response string
        response = string.value.decode().strip()  # Decode the byte string and strip whitespace

        # Check if response contains '=' and extract the value correctly
        if '=' in response:
            response = response.split('=')[-1].strip()  # Take the part after '=' and strip spaces
        
        # Replace commas with dots (if applicable) and convert to float
        float_value = float(response.replace(',', '.'))
        if self.verbose:
            print(f"Target wavelength port {port} = {float_value:.9f}")
        return float_value
    
    def set_pid_course_num(self, port = 1, target_wl = 780.123456789):
        string=f"{target_wl:.9f}\0".replace('.', ',').encode('utf-8')
         #Watch out to use the correct delimiter for your country "." or ","
        ad=ctypes.cast(string, ctypes.POINTER(ctypes.c_char))
        wlmData.dll.SetPIDCourseNum(port,ad)


    #PID functions regarding the output
    def get_deviation_signal(self, port):
        deviation_signal = wlmData.dll.GetDeviationSignalNum(port,0)
        return deviation_signal
    
    def set_deviation_signal(self, port, voltage):
        """sets the voltage in mV to channel port
        returns the set value"""
        deviation_signal = wlmData.dll.SetDeviationSignalNum(port,voltage)
        return deviation_signal
    

    #Assign and unassign channels
    def get_channel_assignment(self, port: int):
        """Returns True if deviation channel `port` is assigned (locked), False otherwise."""
        intval = ctypes.c_long(0)
        doubleval = ctypes.c_double(0)
        wlmData.dll.GetPIDSetting(wlmConst.cmiDeviationChannel, ctypes.c_long(port),
                                  ctypes.byref(intval), ctypes.byref(doubleval))
        # intval == port means assigned; 0 means disabled
        return intval.value == port

    def set_channel_assignment(self, port: int, enable):
        """port is wavemeter port 1-8
        enable is True or False

        if enable is True deviation channel is set to port
        if enable is False deviation channel is set to 0 -- this disables the operation.

        """
        channel_to_set = port if enable else False

        wlmData.dll.SetPIDSetting(wlmConst.cmiDeviationChannel , ctypes.c_long(port),
                                  ctypes.c_long(channel_to_set), 0)
        

        

    
    #get the bounds

    def get_deviation_bounds(self, port):
        var1 = ctypes.c_long()
        deviation_min = ctypes.c_double() #the data is in this
        var3 = ctypes.c_char_p()
        wlmData.dll.GetLaserControlSetting(wlmConst.cmiDeviationBoundsMin,ctypes.c_long(port),
                                        ctypes.byref(var1),
                                        ctypes.byref(deviation_min),
                                        var3)
        
        deviation_max = ctypes.c_double() #the data is in this
        wlmData.dll.GetLaserControlSetting(wlmConst.cmiDeviationBoundsMax,ctypes.c_long(port),
                                ctypes.byref(var1),
                                ctypes.byref(deviation_max),
                                var3)
        
        return deviation_min.value, deviation_max.value

    #TODO: set the bounds
        

    
