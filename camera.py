import time

import numpy as np
import cv2

import multiprocessing
from multiprocessing.shared_memory import SharedMemory, ShareableList
from multiprocessing import Value, Manager
import ctypes


class Camera:

    DISPLAY = True

    image_size: tuple[int, int, int]
    camera_index: int

    def __init__(self, camera_index: int, shared_telemetry: ShareableList | None):

        self.camera_index = camera_index
        self._shared_telemetry = shared_telemetry

        tmp_capture = cv2.VideoCapture(0)
        if not tmp_capture.isOpened():
            raise ConnectionError("Failed to open VideoCapture with index 0")
        ret, image = tmp_capture.read()
        if not ret:
            raise ConnectionError("ret is False")
        tmp_capture.release()

        self.image_size = tuple(image.shape)

        self._shared_image_memory = SharedMemory("Image", create=True,
                                                 size=self.image_size[0] * self.image_size[1] * self.image_size[2] * 8)
        self._shared_hsv_memory = SharedMemory("HSV", create=True,
                                                 size=self.image_size[0] * self.image_size[1] * self.image_size[2] * 8)
        self._shared_image_data = np.ndarray(image.shape, dtype=np.uint8, buffer=self._shared_image_memory.buf)
        self._shared_hsv_data = np.ndarray(image.shape, dtype=np.uint8, buffer=self._shared_hsv_memory.buf)

        self._shared_memory_manager = Manager()
        self._shared_is_releasing = self._shared_memory_manager.Value(ctypes.c_bool, False)

        self._shared_grabber_x = self._shared_memory_manager.Value(ctypes.c_uint16, 0)
        self._shared_grabber_y = self._shared_memory_manager.Value(ctypes.c_uint16, 0)

        self._shared_object_x = self._shared_memory_manager.Value(ctypes.c_uint16, 0)
        self._shared_object_y = self._shared_memory_manager.Value(ctypes.c_uint16, 0)

        self._shared_text = self._shared_memory_manager.Value(ctypes.c_char_p, "")

        self._shared_image_time = self._shared_memory_manager.Value(ctypes.c_uint64, 0)

        self._child_process = multiprocessing.Process(target=Camera.screen_updater, args=(
            self.DISPLAY,
            self.camera_index,
            self.image_size,
            self._shared_image_time,
            self._shared_is_releasing,
            self._shared_grabber_x,
            self._shared_grabber_y,
            self._shared_object_x,
            self._shared_object_y,
            self._shared_text,
            self._shared_telemetry

        ))
        self._child_process.start()


    @staticmethod
    def screen_updater(display: bool,
                       camera_index: int,
                       image_size,
                       image_time: Value,
                       is_releasing: Value,
                       grabber_x: Value,
                       graber_y: Value,
                       object_x: Value,
                       object_y: Value,
                       text: Value,
                       telemetry: ShareableList):

        capture = cv2.VideoCapture(camera_index)
        if not capture.isOpened():
            print(f"Failed to open VideoCapture with index {camera_index}")
            return

        shared_memory = SharedMemory("Image")
        shared_memory_hsv = SharedMemory("HSV")
        shared_image = np.ndarray(image_size, dtype=np.uint8, buffer=shared_memory.buf)
        shared_hsv = np.ndarray(image_size, dtype=np.uint8, buffer=shared_memory_hsv.buf)

        while True:
            ret, image = capture.read()
            t = time.time()
            image_time.value = int(t * 1000)

            if is_releasing.value:
                break

            hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
            np.copyto(shared_image, image)
            np.copyto(shared_hsv, hsv)

            if display:
                if grabber_x.value > 0 and graber_y.value > 0:
                    image = cv2.rectangle(image, (grabber_x.value - 5, graber_y.value - 5), (grabber_x.value + 5, graber_y.value + 5), (255, 255, 0), 2)

                if object_x.value > 0 and object_y.value > 0:
                    image = cv2.rectangle(image, (object_x.value - 5, object_y.value - 5), (object_x.value + 5, object_y.value + 5), (255, 0, 255), 2)

                image = cv2.putText(image, text.value, (5, image.shape[0] - 25),
                                    cv2.FONT_HERSHEY_COMPLEX, 1, (255, 255, 0), 1)

                if telemetry is not None:
                    image = cv2.putText(image, f"Дальномеры: {telemetry[0]} {telemetry[1]}",
                                        (5, 25), cv2.FONT_HERSHEY_COMPLEX, 0.8, (255, 255, 0), 1)
                    image = cv2.putText(image, f"Рука: {telemetry[5]}",
                                        (5, 55), cv2.FONT_HERSHEY_COMPLEX, 0.8, (255, 255, 0), 1)

                cv2.imshow("Robot", image)
                cv2.waitKey(1)

        shared_memory.close()
        cv2.destroyAllWindows()

    def release(self):
        self._shared_is_releasing.value = True

    @property
    def current_image(self) -> np.ndarray:
        return self._shared_image_data

    @property
    def image_time(self) -> int:
        return self._shared_image_time.value

    @property
    def current_image_hsv(self) -> np.ndarray:
        return self._shared_hsv_data

    def draw_grabber_pos(self, pos: tuple[int, int] | None):
        self._shared_grabber_x.value = 0 if pos is None else pos[0]
        self._shared_grabber_y.value = 0 if pos is None else pos[1]

    def draw_object_pos(self, pos: tuple[int, int] | None):
        self._shared_object_x.value = 0 if pos is None else pos[0]
        self._shared_object_y.value = 0 if pos is None else pos[1]

    def set_text(self, text: str):
        self._shared_text.value = text


def test():
    camera = Camera(0, None)

    import grab_helper

    while True:
        image = camera.current_image.copy()
        image_hsv = camera.current_image_hsv.copy()

        grabber_find_area = grab_helper.get_area(image.shape[1], image.shape[0], grab_helper.GRABBER_FIND_AREA)

        grabber_x, grabber_y = grab_helper.find_grabber_center(image_hsv, grabber_find_area)
        camera.draw_grabber_pos((grabber_x, grabber_y))

        cube_find_area = grab_helper.get_area(image.shape[1], image.shape[0], grab_helper.CUBE_FIND_AREA)
        cube_x, cube_y, rotated = grab_helper.find_cube(image_hsv, cube_find_area, "green")
        camera.set_text(f"{cube_x} {cube_y}")

        if None not in (cube_x, cube_y, rotated):
            camera.draw_object_pos((cube_x, cube_y))

        cv2.waitKey(1)

if __name__ == "__main__":
    test()
