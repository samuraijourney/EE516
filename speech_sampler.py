import matplotlib.pyplot as plt
import numpy as np
import sounddevice as sd
import fractions
import time
import ctypes
import multiprocessing
from scipy import signal
from matplotlib import animation
from pathos.multiprocessing import ProcessingPool as Pool
from pathos.pools import ProcessPool, ThreadPool

class Speech_Sampler():

    def __init__(self, window_duration = 2):
        self.__window_duration = window_duration # seconds
        self.__fs = 16000 # time resolution of the recording device (Hz)

        length = self.__window_duration * self.__fs

        self.__data_update_interval = 0.025 # seconds
        self.__signal_plot_refresh_interval = 0.025 # seconds
        self.__spectrogram_plot_refresh_interval = 0.5 # seconds
        self.__energy_plot_refresh_interval = 0.025 # seconds
        self.__zero_crossings_plot_refresh_interval = 0.025 # seconds
        self.__pause_stop_draw_duration = 0.5 # seconds
        self.__silence_samples = int(0.01 * self.__fs)
        self.__data = np.zeros(length)
        self.__force_draw = False
        self.__hide_signal_plot = False
        self.__hide_spectrogram_plot = False
        self.__hide_energy_plot = False
        self.__hide_zero_crossing_plot = False
        self.__last_pause_state = False
        self.__last_speech_detection_index = -1
        self.__last_updated_spectrogram_time = 0
        self.__last_updated_signal_time = 0
        self.__last_updated_energy_time = 0
        self.__last_updated_zero_crossings_time = 0
        self.__pause = self.__last_pause_state
        self.__silence_energy = np.zeros(length)
        self.__silence_energy_min = np.infty
        self.__silence_energy_max = 0
        self.__silence_energy_max_thresholds = np.zeros(length)
        self.__silence_energy_min_thresholds = np.zeros(length)
        self.__silence_zero_crossings = np.zeros(length)
        self.__silence_speech_detect = np.zeros(length)
        self.__silence_zero_crossing_threshold = 0
        self.__silence_sample_count = 0
        self.__silence_threshold_samples_speech_detection = 100 * self.__silence_samples
        self.__silence_threshold_samples = 100 * self.__silence_samples
        self.__silence_std_deviation = 0
        self.__time = np.linspace(-self.__window_duration, 0, length)
        self.__tracking_index = int(length - self.__silence_samples / 2 - 1)

    def __audio_callback(self, indata, frames, time, status):
        data = indata[:, 0]
        shift = len(data)

        self.__data = np.roll(self.__data, -shift, axis = 0)
        self.__data[-shift:] = data[:]
        self.__tracking_index = self.__tracking_index - shift
        self.__last_speech_detection_index = self.__last_speech_detection_index - shift

        self.__silence_energy = np.roll(self.__silence_energy, -shift, axis = 0)
        self.__silence_energy[-shift:] = 0
        self.__silence_energy_max_thresholds = np.roll(self.__silence_energy_max_thresholds, -shift, axis = 0)
        self.__silence_energy_max_thresholds[-shift:] = 0
        self.__silence_energy_min_thresholds = np.roll(self.__silence_energy_min_thresholds, -shift, axis = 0)
        self.__silence_energy_min_thresholds[-shift:] = 0
        self.__silence_zero_crossings = np.roll(self.__silence_zero_crossings, -shift, axis = 0)
        self.__silence_zero_crossings[-shift:] = 0
        self.__silence_speech_detect = np.roll(self.__silence_speech_detect, -shift, axis = 0)
        self.__silence_speech_detect[-shift:] = 0

        signal_samples_radius = self.__silence_samples / 2
        signal_start_index = self.__tracking_index
        signal_end_index = self.__tracking_index + shift

        signal_samples = self.__build_sample_matrix(self.__data, signal_start_index, signal_end_index, signal_samples_radius * 2)

        # Compute energy
        self.__silence_energy[-shift:] = self.__calculate_energy(signal_samples)
        
        # Compute zero crossings    
        self.__silence_zero_crossings[-shift:] = self.__calculate_zero_crossings(signal_samples)

        # Check for speech  
        if (self.__silence_sample_count >= self.__silence_threshold_samples) and (np.mod(self.__silence_sample_count, 10 * shift) == 0):
            
            # Compute energy thresholds
            self.__silence_energy_min = np.min(self.__silence_energy[-10 * shift:])
            self.__silence_energy_max = np.max(self.__silence_energy[-10 * shift:])
            energy_thresholds = self.__calculate_energy_threshold(self.__silence_energy_min, self.__silence_energy_max)
            
            index = max(-self.__silence_threshold_samples_speech_detection, self.__last_speech_detection_index)
            energies = self.__silence_energy[index:]
            zero_crossings = self.__silence_zero_crossings[index:]
            n1, n2 = self.__find_speech_segment(energies, zero_crossings, energy_thresholds[0], energy_thresholds[1], self.__silence_zero_crossing_threshold, len(energies))
            if not np.isnan(n1):
                self.__silence_speech_detect[n1] = 1
                self.__silence_speech_detect[n2] = 1
                self.__silence_energy_min_thresholds[n1 : n2] = energy_thresholds[0]
                self.__silence_energy_max_thresholds[n1 : n2] = energy_thresholds[1]
                self.__silence_energy_min = np.infty
                self.__silence_energy_max = 0
                self.__last_speech_detection_index = n2

        # Compute zero-crossing thresholds
        if (self.__silence_sample_count < self.__silence_threshold_samples) and ((self.__silence_sample_count + shift) >= self.__silence_threshold_samples):
            zero_crossings_samples = self.__silence_zero_crossings[signal_end_index - self.__silence_threshold_samples : signal_end_index]
            self.__silence_zero_crossing_threshold = self.__calculate_zero_crossing_threshold(zero_crossings_samples)

        self.__tracking_index = self.__tracking_index + shift
        self.__silence_sample_count = self.__silence_sample_count + shift

    def __build_sample_matrix(self, data, start_index, end_index, samples_per_column):
        matrix = np.zeros((samples_per_column, end_index - start_index))
        radius = samples_per_column / 2
        for i in range(start_index, end_index):
            matrix[:, i - start_index] = data[i - radius : i + radius]
        return matrix

    def __calculate_energy(self, data):
        return np.sum(np.abs(data), axis = 0)

    def __calculate_energy_threshold(self, min_energy, max_energy):
        min_threshold_energy = np.min((0.03 * (max_energy - min_energy) + min_energy, 4 * min_energy), axis = 0)
        max_threshold_energy = 10 * min_threshold_energy

        return np.stack((min_threshold_energy, max_threshold_energy), axis = 0)

    def __calculate_zero_crossings(self, data):
        zero_crossings = 0

        data_roll = np.roll(data, 1, axis = 0)
        zero_crossings = np.sum(np.abs(np.subtract(np.sign(data[1:, :]), np.sign(data_roll[1:, :]))), axis = 0) / (2 * len(data))
        
        return zero_crossings

    def __calculate_zero_crossing_threshold(self, zero_crossings):
        return min(25, np.mean(zero_crossings) + 2 * np.std(zero_crossings))

    def __create_plots(self):
        plot_count = 0
        plot_index = 0

        if (not self.__hide_signal_plot):
            plot_count = plot_count + 1
        if (not self.__hide_energy_plot):
            plot_count = plot_count + 1
        if (not self.__hide_zero_crossing_plot):
            plot_count = plot_count + 1
        if (not self.__hide_spectrogram_plot):
            plot_count = plot_count + 1

        self.__fig, axes = plt.subplots(plot_count, 1)

        # Initialize all plots
        if (not self.__hide_signal_plot):
            self.__initialize_signal_plot(axes[plot_index], self.__data[:], self.__silence_speech_detect[:])
            plot_index = plot_index + 1
        if (not self.__hide_energy_plot):
            self.__initialize_energy_plot(axes[plot_index], self.__silence_energy[:], self.__silence_energy_min_thresholds[:], self.__silence_energy_max_thresholds[:])
            plot_index = plot_index + 1
        if (not self.__hide_zero_crossing_plot):
            self.__initialize_zero_crossings_plot(axes[plot_index], self.__silence_zero_crossings[:])
            plot_index = plot_index + 1
        if (not self.__hide_spectrogram_plot):
            self.__initialize_spectrogram_plot(axes[plot_index], self.__data[:])
            axes[plot_index].axis('off')

        self.__fig.tight_layout(pad = 0)

    def __find_speech_segment(self, energies, zero_crossings, energy_min_threshold, energy_max_threshold, zero_crossing_threshold, max_distance):
        n1 = np.nan
        n2 = np.nan
        index = -1
        lookahead = int(0.25 * self.__fs)

        while index > -max_distance:

            # Find where we dip below the ITL from the end
            for i in range(-index - 1, max_distance):
                index = -i - 1
                if energies[index] < energy_min_threshold:
                    break

            if index <= -max_distance:
                return np.nan, np.nan

            # Find where we go above ITL from the end      
            for i in range(-index - 1, max_distance):
                index = -i - 1
                if energies[index] > energy_min_threshold:
                    break

            if index <= -max_distance:
                return np.nan, np.nan

            n2 = index

            # Find where we exceed ITU
            for i in range(-index - 1, max_distance):
                index = -i - 1
                # Dipped under the min threshold before exceeding max
                if energies[index] < energy_min_threshold:
                    break
                if energies[index] > energy_max_threshold:
                    break

            if energies[index] < energy_min_threshold:
                continue
            if energies[index] > energy_max_threshold:
                break

        if index <= -max_distance:
            return np.nan, np.nan

        # Find where we dip below ITL
        for i in range(-index - 1, max_distance):
            index = -i - 1
            # Dipped under the min threshold before exceeding max
            if energies[index] < energy_min_threshold:
                break

        # Not enough data to search zero crossings in back
        if index - lookahead < -max_distance:
            return np.nan, np.nan
        
        n1 = index
        index_start = n1
        index_end = n2
        zc_start_count = 0
        zc_end_count = 0

        # Look for fricatives
        for i in range(0, lookahead):
            zc_start = np.max(np.sign(zero_crossings[index_start - i] - zero_crossing_threshold), 0)
            zc_start_count = zc_start_count + zc_start
            zc_end = np.max(np.sign(zero_crossings[index_end + i] - zero_crossing_threshold), 0)
            zc_end_count = zc_end_count + zc_end

            # Move N1 to account for trailing fricatives
            if (zc_start == 1) and (zc_start_count >= 3):
                n1 = index_start - i
            # Move N2 to account for leading fricatives
            if (zc_end == 1) and (zc_end_count >= 3):
                n2 = index_end + i

        return n1, n2

    def __initialize_energy_plot(self, ax, data, min_threshold_data, max_threshold_data):
        self.__energy_plot = ax
        self.__energy_plot_data = ax.plot(self.__time, data)
        self.__energy_plot_min_data = ax.plot(self.__time, min_threshold_data, color='r')
        self.__energy_plot_max_data = ax.plot(self.__time, max_threshold_data, color='g')

        ax.axis((0, len(data), 0, 1))
        ax.set_title("Energy")
        ax.xaxis.grid(True)
        ax.yaxis.grid(True)
        ax.set_xlabel("Time (s)")
        ax.set_xlim([-self.__window_duration, 0])
        ax.tick_params(bottom='on', top='off', labelbottom='on', right='off', left='on', labelleft='on')

    def __initialize_signal_plot(self, ax, signal_data, speech_detect_data):        
        self.__signal_plot = ax
        self.__signal_plot_data = ax.plot(self.__time, signal_data)
        self.__signal_plot_speech_detect_data = ax.plot(self.__time, speech_detect_data, color='r')
        self.__garbage_plot_data = ax.plot(self.__time, np.full(len(signal_data), np.nan))

        ax.set_title("Audio")
        ax.axis((0, len(signal_data), -0.25, 0.25))
        ax.xaxis.grid(True)
        ax.yaxis.grid(True)
        ax.set_xlabel("Time (s)")
        ax.set_xlim([-self.__window_duration, 0])
        ax.tick_params(bottom='on', top='off', labelbottom='on', right='off', left='on', labelleft='on')

    def __initialize_spectrogram_plot(self, ax, data):
        self.__spectrogram_plot = ax

        ax.set_title("Spectrogram")
        ax.specgram(data, NFFT = 1024, Fs = self.__fs, noverlap = 900)

    def __initialize_zero_crossings_plot(self, ax, data):
        self.__silence_zero_crossings_plot = ax
        self.__silence_zero_crossings_plot_data = ax.plot(self.__time, data)
        self.__silence_zero_crossings_plot_threshold_data = ax.plot(self.__time, np.full(len(data), self.__silence_zero_crossing_threshold), color='r')

        ax.axis((0, len(data), 0, 0.1))
        ax.set_title("Zero Crossings")
        ax.xaxis.grid(True)
        ax.yaxis.grid(True)
        ax.set_xlabel("Time (s)")
        ax.set_xlim([-self.__window_duration, 0])
        ax.tick_params(bottom='on', top='off', labelbottom='on', right='off', left='on', labelleft='on')

    def __scale_plot(self, ax, data, grow_only = True):
        max_val = max(data)
        min_val = min(data)
        padding = (max_val - min_val) * 0.1

        if padding == 0.0:
            padding = 0.05

        if grow_only == True:
            min_val = min(ax.get_ylim()[0], min_val - padding)
            max_val = max(ax.get_ylim()[1], max_val + padding)
        else:
            min_val = min_val - padding
            max_val = max_val + padding

        ax.set_ylim(bottom = min_val, top = max_val)

    def __update_energy_plot(self, data, min_threshold_data, max_threshold_data):
        self.__scale_plot(self.__energy_plot, data, False)
        self.__scale_plot(self.__energy_plot, min_threshold_data, True)
        self.__scale_plot(self.__energy_plot, max_threshold_data, True)

        for _,line in enumerate(self.__energy_plot_data):
            line.set_ydata(data)
        for _,line in enumerate(self.__energy_plot_min_data):
            line.set_ydata(min_threshold_data)
        for _,line in enumerate(self.__energy_plot_max_data):
            line.set_ydata(max_threshold_data)

    def __update_plots(self, frame):
        if self.__force_draw == True:
            self.__animation.event_source.stop()

        if ((not self.__hide_signal_plot) and (((time.time() - self.__last_updated_signal_time) > self.__signal_plot_refresh_interval) or self.__force_draw)):
            self.__update_signal_plot(self.__data[:], self.__silence_speech_detect[:])
            self.__last_updated_signal_time = time.time()

        if ((not self.__hide_energy_plot) and (((time.time() - self.__last_updated_energy_time) > self.__energy_plot_refresh_interval) or self.__force_draw)):
            self.__update_energy_plot(self.__silence_energy[:], self.__silence_energy_min_thresholds[:], self.__silence_energy_max_thresholds[:])
            self.__last_updated_energy_time = time.time()

        if ((not self.__hide_zero_crossing_plot) and (((time.time() - self.__last_updated_zero_crossings_time) > self.__zero_crossings_plot_refresh_interval) or self.__force_draw)):
            self.__update_zero_crossings_plot(self.__silence_zero_crossings[:])
            self.__last_updated_zero_crossings_time = time.time()

        if ((not self.__hide_spectrogram_plot) and (((time.time() - self.__last_updated_spectrogram_time) > self.__spectrogram_plot_refresh_interval) or self.__force_draw)):
            self.__update_spectrogram_plot(self.__data[:])
            self.__last_updated_spectrogram_time = time.time()

        if (not self.__pause) or self.__force_draw:
            plt.pause(0.01)

        if (self.__force_draw == True):
            self.__force_draw = False

        if (self.__last_pause_state == False) and (self.__pause == True):
            self.__force_draw = True

        self.__last_pause_state = self.__pause

        return self.__garbage_plot_data

    def __update_signal_plot(self, signal_data, detect_data):
        self.__scale_plot(self.__signal_plot, signal_data, False)
        for _,line in enumerate(self.__signal_plot_data):
            line.set_ydata(signal_data)
        for _,line in enumerate(self.__signal_plot_speech_detect_data):
            line.set_ydata(detect_data)

    def __update_spectrogram_plot(self, data):
        self.__spectrogram_plot.clear()
        self.__spectrogram_plot.specgram(data, NFFT = 1024, Fs = self.__fs, noverlap = 900)
        self.__spectrogram_plot.tick_params(bottom='off', top='off', labelbottom='off', right='off', left='on', labelleft='on')
        self.__spectrogram_plot.set_title("Spectrogram")

    def __update_zero_crossings_plot(self, data):
        threshold_data = np.full(len(data), self.__silence_zero_crossing_threshold)

        self.__scale_plot(self.__silence_zero_crossings_plot, data, False)
        self.__scale_plot(self.__silence_zero_crossings_plot, threshold_data, True)

        for _,line in enumerate(self.__silence_zero_crossings_plot_data):
            line.set_ydata(data)
        for _,line in enumerate(self.__silence_zero_crossings_plot_threshold_data):
            line.set_ydata(threshold_data)

    def hide_energy_plot(self, hide = True):
        self.__hide_energy_plot = hide

    def hide_signal_plot(self, hide = True):
        self.__hide_signal_plot = hide

    def hide_spectrogram_plot(self, hide = True):
        self.__hide_spectrogram_plot = hide

    def hide_zero_crossing_plot(self, hide = True):
        self.__hide_zero_crossing_plot = hide

    def pause(self):
        self.__pause = True

    def resume(self):
        self.__pause = False
        self.__animation.event_source.start()

    def start(self):
        self.__create_plots()
        
        # Need reference to animation otherwise garbage collector removes it...
        self.__animation = animation.FuncAnimation(self.__fig, self.__update_plots, interval = self.__data_update_interval * 1000, blit = True)
        stream = sd.InputStream(channels=1, samplerate=self.__fs, callback=self.__audio_callback)

        with stream:
            plt.show(block = False)
            print("")
            while True:
                raw_input("Press any key to pause")
                self.pause()
                raw_input("Press any key to resume")
                self.resume()

if __name__ == '__main__':
    sampler = Speech_Sampler(5)
    sampler.hide_spectrogram_plot()
    #sampler.hide_energy_plot()
    #sampler.hide_zero_crossing_plot()
    sampler.start()