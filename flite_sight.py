#!/usr/bin/env python

import asyncio
import argparse
import json
import websockets
from datetime import datetime


class Veriflite_Receiver():
    def __init__(self, onImpact, onDepart, onIdle):
        self.onImpact = onImpact
        self.onDepart = onDepart
        self.onIdle = onIdle

    def parse(self, message):
        try:
            j = json.loads(message)
        except json.decoder.JSONDecodeError:
            print(f"::: {message}")
            return

        print(f"{j['Address']}, {j['Type']}, {j['Data']}")
        if 'IMPACT' in j['Type']:
            self.onImpact(j['Address'])

        if 'DEPART' in j['Type']:
            self.onDepart(j['Address'])

        if 'IDLE' in j['Type']:
            self.onIdle(j['Address'])


class Camera():
    def __init__(self, device, resolution, fps, playbackDelay):
        self.recording = False
        self.count = 0
        self.mpvLock = asyncio.Lock()
        self.currentVid = None
        self.lastVid = None
        self.device = device
        self.fps = fps
        self.resolution = resolution
        self.playbackDelay = playbackDelay

    async def record(self):
        if self.recording:
            return
        self.recording = True
        self.count += 1
        time = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.currentVid = f"{time}_{self.count:03}.mp4"
        print(f"Recording video: {self.currentVid}")
        self.ffmpeg = await asyncio.create_subprocess_shell(
            f"ffmpeg -hide_banner -loglevel error -y -f v4l2 -framerate {self.fps} -video_size {self.resolution} -input_format mjpeg -i {self.device} -c:v h264 -preset faster -qp 0 -strict -2 {self.currentVid}")
        await self.ffmpeg.wait()

    def stop(self):
        if not self.recording:
            return
        print("Stop recording")
        self.recording = False
        try:
            self.ffmpeg.terminate()
        except ProcessLookupError as e:
            print(f"ProcessLookupError: {e}")
        self.lastVid = self.currentVid

    async def play(self):
        print(f"Playing video: {self.lastVid}")
        await asyncio.sleep(self.playbackDelay)
        async with self.mpvLock:
            mpv = await asyncio.create_subprocess_shell(
                f"mpv --fullscreen --quiet {self.lastVid}")
            await mpv.wait()


class Flite_Sight():
    def __init__(self, uri, device, resolution, fps, playbackDelay):
        self.uri = uri
        self.cam = Camera(device, resolution, fps, playbackDelay)
        self.vf = Veriflite_Receiver(self.onImpact, self.onDepart, self.onIdle)
        self.recordingSensor = None

    async def start(self):
        print(f"Opening connection to Veriflite portal: {self.uri}")
        async for websocket in websockets.connect(self.uri, open_timeout=5):
            while True:
                try:
                    pkt = await websocket.recv()
                except websockets.ConnectionClosed as e:
                    print(f"Websockets error: {e}")
                    self.cam.stop()
                    await asyncio.sleep(2)
                    break
                else:
                    self.vf.parse(pkt)
            print("Reconnecting...")

    def onImpact(self, address):
        if not self.recordingSensor:
            self.recordingSensor = address
            asyncio.get_running_loop().create_task(self.cam.record())

    def onDepart(self, address):
        pass

    def onIdle(self, address):
        if self.recordingSensor and address in self.recordingSensor:
            self.recordingSensor = None
            self.cam.stop()
            asyncio.get_running_loop().create_task(self.cam.play())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-i", "--ip", type=str, help="The IP address of the Veriflite portal")
    parser.add_argument("-v", "--device", type=str, default="/dev/video0", help="The video device to use (defaults to /dev/video0)")
    parser.add_argument("-d", "--delay", type=int, default=5, help="Delay after recording before video (deafults to 5 seconds)")
    parser.add_argument("-f", "--fps", type=int, default=30, help="Frame rate to record from the webcam")
    parser.add_argument("-r", "--res", type=str, default="1920x1080", help="Video resolution to record from the webcam")
    args = parser.parse_args()

    fliteSight = Flite_Sight(f"ws://{args.ip}:4651/raw", args.device, args.res, args.fps, args.delay)
    while True:
        asyncio.run(fliteSight.start())

