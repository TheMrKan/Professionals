
# -*- coding: utf-8 -*-
import serial
from serial.serialutil import SerialException

import time
import math

import multiprocessing
from multiprocessing.managers import SharedMemoryManager
from multiprocessing.shared_memory import ShareableList
from multiprocessing import Value, Manager, Event
import ctypes


class SerialRobot:

    _telemetry_len: int

    _shared_memory_manager: Manager
    _shared_telemetry: ShareableList
    _shared_command: Value
    _shared_confirmations: Value

    _on_serial_ready: Event
    _on_command_sent: Event
    _on_command_completed: Event
    _on_releasing: Event
    _on_telemetry_updated: Event

    _serial_io: multiprocessing.Process

    _watcher: multiprocessing.Process
    _watcher_emergency_stop_distance: Value

    RANGEFINDER_FORWARD = 0
    RANGEFINDER_RIGHT = 1
    _RANGEFINDER_ANGLES = {RANGEFINDER_FORWARD: 110, RANGEFINDER_RIGHT: 10}

    _rangefinder_direction: int

    def __init__(self, port):
        self._telemetry_len = 7
        self._speed = 24.7436
        self._rangefinder_direction = SerialRobot.RANGEFINDER_FORWARD
        self._permanent_correction = 0

        self._shared_memory_manager = Manager()
        self._shared_telemetry = ShareableList([0] * self._telemetry_len)
        self._shared_command = self._shared_memory_manager.Value(ctypes.c_char_p, "")
        self._shared_confirmations = self._shared_memory_manager.Value(ctypes.c_uint8, 1)

        self._on_serial_ready = Event()
        self._on_command_sent = Event()
        self._on_command_completed = Event()
        self._on_releasing = Event()
        self._on_telemetry_updated = Event()

        self._serial_io = multiprocessing.Process(target=SerialRobot.serial_io, args=(
            port,
            self._shared_telemetry,
            self._shared_command,
            self._shared_confirmations,
            self._on_serial_ready,
            self._on_command_sent,
            self._on_command_completed,
            self._on_releasing,
            self._on_telemetry_updated))

        self._serial_io.start()

        self._on_serial_ready.wait()

        self._watcher_status = self._shared_memory_manager.Value(ctypes.c_uint8, 0)
        self._watcher_left_correct_min = self._shared_memory_manager.Value(ctypes.c_uint16, 0)
        self._watcher_left_correct_max = self._shared_memory_manager.Value(ctypes.c_uint16, 0)
        self._watcher_target_distance = self._shared_memory_manager.Value(ctypes.c_uint16, 0)
        self._watcher_command = self._shared_memory_manager.Value(ctypes.c_char_p, "")

        self._watcher = multiprocessing.Process(target=SerialRobot.watcher, args=(
            self._permanent_correction,
            self._on_releasing,
            self._on_telemetry_updated,
            self._on_command_completed,
            self._on_command_sent,
            self._shared_telemetry,
            self._shared_command,
            self._shared_confirmations,
            self._watcher_status,
            self._watcher_left_correct_min,
            self._watcher_left_correct_max,
            self._watcher_target_distance,
            self._watcher_command
        ))
        self._watcher.start()

        print("Serial robot is ready")

    @staticmethod
    def serial_io(port: str,
                  shared_telemetry: ShareableList,
                  shared_command: Value,
                  shared_confirmations: Value,
                  on_serial_ready: Event,
                  on_command_sent: Event,
                  on_command_completed: Event,
                  on_releasing: Event,
                  on_telemetry_updated: Event):
        trying = 3
        while trying > 0:
            try:
                ser = serial.Serial(port, 115200, timeout=5)
                break
            except SerialException:
                print("Connecting to serial failed")
            time.sleep(2)
            trying -= 1

        if trying <= 0:
            print("Failed to connect to serial")
            return

        on_serial_ready.set()
        confirmations_left = shared_confirmations.value
        waiting_for_sending = ""
        while ser.is_open:
            try:
                bdata = ser.readline()
                data = bdata.decode().strip()

                print(f"SERIAL >>> {data}")

                if on_releasing.is_set():
                    break

                if data == "OK":
                    confirmations_left -= 1
                    print(f"SERIAL CONFIRMED >>> {confirmations_left} LEFT")
                    if confirmations_left <= 0:
                        on_command_completed.set()
                        shared_confirmations.value = 1
                elif data.startswith("+"):
                    print(f"SEND SERIAL CONFIRMED >>> {data}")
                    if data[1:].strip() == waiting_for_sending:
                        on_command_sent.set()
                        waiting_for_sending = ""
                    else:
                        shared_command.value = waiting_for_sending
                else:
                    splitted = data.split(" ")
                    if any(splitted):
                        bad_packet = False
                        for i in range(len(splitted)):
                            if splitted[i].isdigit():
                                shared_telemetry[i] = int(splitted[i])
                            else:
                                bad_packet = True
                                break
                        if not bad_packet:
                            on_telemetry_updated.set()

                if shared_command.value:
                    print(f"SEND SERIAL >>> {shared_command.value}")
                    waiting_for_sending = shared_command.value
                    ser.write(shared_command.value.encode("ascii"))
                    #print(f"SET CONFIRMATIONS: {shared_confirmations.value}")
                    confirmations_left = shared_confirmations.value
                    shared_command.value = ""
            except KeyboardInterrupt:
                break

        ser.close()

    @staticmethod
    def watcher(
            permanent_correction: float,
            on_releasing: Event,
            on_telemetry_updated: Event,
            on_command_completed: Event,
            on_command_sent: Event,
            telemetry: ShareableList,
            shared_command: Value,
            shared_confirms: Value,
            status: Value,
            left_correct_min: Value,
            left_correct_max: Value,
            target_distance: Value,
            last_command: Value,
    ):

        is_correcting = False
        left_distance_buffer = []
        left_distance_history = []
        last_correct = 0
        last_distance = -1
        previous_speed_correction = 0

        correction_interval = 0.1
        buffer_size = 10
        target_distance = 16
        p_mult = 250
        d_mult = 100
        found_wall = False
        found_wall_counter = 0

        while not on_releasing.is_set():
            on_telemetry_updated.wait()
            on_telemetry_updated.clear()

            t = time.time()

            is_correcting = status.value == 0 and (left_correct_min.value != 0 or left_correct_max.value != 0)
            if not is_correcting:
                left_distance_buffer.clear()
                left_distance_history.clear()
                previous_speed_correction = 0
                last_correct = 0
                last_distance = -1
                found_wall = False
                found_wall_counter = 0
            
            if is_correcting:
                time_delta = t - last_correct
                if time_delta >= correction_interval:
                    average_left_distance = telemetry[1] / 10
                    if average_left_distance < 60:
                        found_wall_counter += 1
                        found_wall = found_wall or found_wall_counter > 3
                    else:
                        found_wall_counter = 0

                    distance_error = target_distance - average_left_distance

                    if found_wall:
                        left_distance_buffer.append(distance_error)

                        if len(left_distance_buffer) > buffer_size:
                            del left_distance_buffer[0]

                        if len(left_distance_buffer) >= buffer_size :
                            if last_distance != -1 and last_correct != 0:
                                p_sign = 1
                                d_sign = 1

                                d_error = (left_distance_buffer[-1] - left_distance_buffer[0]) * d_mult * d_sign
                                p_error = distance_error * p_mult * p_sign

                                speed_correction = p_error + d_error
                                print("Correction", target_distance - average_left_distance, speed_correction)

                                if speed_correction != previous_speed_correction:
                                    on_command_sent.clear()
                                    shared_command.value = f"V{int(speed_correction)}"
                                    on_command_sent.wait()

                                previous_speed_correction = speed_correction

                            last_correct = t
                            last_distance = average_left_distance


    @property
    def telemetry(self) -> list[int]:
        return list(self._shared_telemetry)

    @property
    def forward_distance(self) -> int:
        return self._shared_telemetry[0] if self._rangefinder_direction == SerialRobot.RANGEFINDER_FORWARD else -1

    @property
    def left_distance(self) -> float:
        return self._shared_telemetry[1] / 10

    @property
    def right_distance(self) -> int:
        return self._shared_telemetry[0] if self._rangefinder_direction == SerialRobot.RANGEFINDER_RIGHT else -1

    @property
    def hand_angle(self) -> int:
        return self._shared_telemetry[5]

    def send_command(self, command: str,
                     await_sending: bool = True,
                     await_completion: bool = False,
                     await_completion_timeout: float = 20,
                     required_confirmations: int = 1):
        self._on_command_sent.clear()
        self._on_command_completed.clear()
        self._shared_confirmations.value = required_confirmations
        self._shared_command.value = command

        if await_sending and not await_completion:
            self._on_command_sent.wait()

        if await_completion:
            self._on_command_completed.wait(timeout=await_completion_timeout)

    def go(self, distance: int, correct: bool = False, *args, wall_distance: int = 0):
        print(f"Going {distance if wall_distance == 0 else 'to wall ' + str(wall_distance)} {'(correction)' if correct else ''}")

        if wall_distance == 0:
            cmd = f"F{distance * 10}"
        else:
            cmd = f"W{wall_distance * 10}"
            self.switch_rangefinder(SerialRobot.RANGEFINDER_FORWARD)

        if correct:
            self._watcher_left_correct_min.value = -5
            self._watcher_left_correct_max.value = 5
            self._watcher_target_distance.value = distance
            self._watcher_command.value = cmd

        if self._permanent_correction != 0:
            self.send_command(f"V{int(self._permanent_correction)}")
        self.send_command(cmd, await_completion=True, required_confirmations=1)

        while self._watcher_status.value == 2:
            self._on_command_completed.wait()

        self._watcher_left_correct_min.value = 0
        self._watcher_left_correct_max.value = 0
        self._watcher_target_distance.value = 0

        if wall_distance > 0 and self.forward_distance - wall_distance > 10:
            self.go(distance, correct=correct, wall_distance=wall_distance)

    def rotate(self, degrees: int, wait: bool = True):
        print(f"Rotating: {degrees}")

        timeout = 20
        if abs(degrees) < 10:
            timeout = 1

        self.send_command(f"R{degrees}", await_completion=wait, required_confirmations=1, await_completion_timeout=timeout)

    def reset_position(self):
        self.send_command("N")

    def close_grabber(self):
        self.send_command("H0", await_completion=True, required_confirmations=1)
        #time.sleep(4)

    def open_grabber(self):
        self.send_command("H2", await_completion=True, required_confirmations=1)
        #time.sleep(4)

    def set_red_led(self, status: bool):
        self.send_command(f"G{int(status)}")

    def set_green_led(self, status: bool):
        self.send_command(f"L{int(status)}")

    def set_led_freq(self, millis: int):
        self.send_command(f"Q{millis}")

    def set_hand_angle(self, degrees: int):
        self.send_command(f"S{degrees}", await_completion=self.hand_angle != degrees)    # OK не приходит, если отправлен тот же угол

    def switch_rangefinder(self, direction: int, force: bool = False):
        """
        Поворачивает ультразвуковой дальномер
        :param direction: 0 - прямо, 1 - направо
        :param force: если True, то команда отправится даже если положения совпадают
        """
        if not force and self._rangefinder_direction == direction:
            return

        angle = SerialRobot._RANGEFINDER_ANGLES[direction]
        self._rangefinder_direction = direction
        self.send_command(f"Y{angle}")

        time.sleep(0.8)   # подтверждение выполнения на эту команду не работает, поэтому просто задержкой

    def set_light(self, enabled: bool):
        self.send_command(f"B{int(enabled)}")

    def release(self):
        self.set_hand_angle(125)
        self.reset_position()
        self._on_releasing.set()


if __name__ == "__main__":
    robot = SerialRobot("/dev/ttyAMA0")

    robot.set_hand_angle(117)

    #robot.release()
    exit()
