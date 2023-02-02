#!/usr/bin/env python

import argparse
import asyncio
import json
import srt
import subprocess
import time
import websockets
from datetime import datetime, timedelta


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

        if j['event'] == 'packet':
            pkt = j['data']
            vfEvent = pkt['type']
            address = pkt['address']
            seq = pkt['sequenceNumber']
            data = pkt['data']
            print(f"{address}, {vfEvent}, {data}")
            if 'IMPACT' in vfEvent:
                self.onImpact(address, data)

            if 'DEPART' in vfEvent:
                self.onDepart(address, data)

            if 'IDLE' in vfEvent:
                self.onIdle(address, data)


class Camera():
    def __init__(self, device, resolution, fps, playbackDelay):
        self.recording = False
        self.mpvLock = asyncio.Lock()
        self.currentVid = None
        self.device = device
        self.fps = fps
        self.resolution = resolution
        self.playbackDelay = playbackDelay

    async def record(self, filename):
        if self.recording:
            return
        self.recording = True
        self.currentVid = filename
        self.currentVidStartTime = datetime.now()
        print(f"Recording video: {self.currentVid}")
        ffmpegCmd = f"ffmpeg -loglevel warning -hide_banner -y -f v4l2 -framerate {self.fps} -video_size {self.resolution} -input_format mjpeg -i {self.device} -c:v h264 -preset faster -qp 0 -strict -2 {self.currentVid}"
        print(ffmpegCmd)
        self.ffmpeg = await asyncio.create_subprocess_shell(ffmpegCmd)
        await self.ffmpeg.wait()

    def getCurrentTimestamp(self):
        if not self.recording:
            return 0
        dt = datetime.now() - self.currentVidStartTime
        return dt.total_seconds()

    def stop(self):
        if not self.recording:
            return 0
        print("Stop recording")

        stopTime = self.getCurrentTimestamp()
        self.recording = False
        try:
            self.ffmpeg.terminate()
        except ProcessLookupError as e:
            print(f"ProcessLookupError: {e}")
            return 0

        duration = 0
        for i in range(30):
            try:
                ffprobe = f"ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 {self.currentVid}"
                duration = float(subprocess.run(ffprobe, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT).stdout)
            except (FileNotFoundError, ValueError) as e:
                time.sleep(1)

        syncError = duration - stopTime
        return syncError

    async def play(self, videoFile):
        print(f"Playing video: {videoFile}")
        await asyncio.sleep(self.playbackDelay)
        async with self.mpvLock:
            mpv = await asyncio.create_subprocess_shell(
                f"mpv --fullscreen --quiet {videoFile}")
            await mpv.wait()


class Subtitler():
    def __init__(self, filename):
        self.filename = filename
        self.subtitles = []

    def append(self, departTime, impactTime, tof):
        print(f"Subtitler.append({departTime}, {impactTime}, {tof}")
        self.subtitles.append(srt.Subtitle(index=len(self.subtitles) + 1,
                                           start=timedelta(seconds=departTime),
                                           end=timedelta(seconds=impactTime),
                                           # content="ToF: {:.3f} ({:.3f})  {}".format(tofs[-1], tofs[-1] - tofs[-2], total_tof)))
                                           content=f"{tof:.3f}"))

    def timeshift(self, delta):
        print(f"Shifting subtitles by {delta} seconds")
        for s in self.subtitles:
            s.start += timedelta(seconds=delta)
            s.end += timedelta(seconds=delta)

    def save(self):
        print(f"Saving ToF subtitles to {self.filename}")
        with open(self.filename, "w") as f:
            f.write(srt.compose(self.subtitles))


class Flite_Sight():
    def __init__(self, uri, device, resolution, fps, playbackDelay, subsEnabled):
        self.uri = uri
        self.subsEnabled = subsEnabled
        self.cam = Camera(device, resolution, fps, playbackDelay)
        self.vf = Veriflite_Receiver(self.onImpact, self.onDepart, self.onIdle)
        self.recordingSensor = None
        self.tof = 0
        self.fileprefix = None
        self.departTime = 0

        # Only tracking the recording sensor!!
        self.lastImpactTime = 0
        self.lastDepartTime = 0
        self.lastIdleTime = 0

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

    def onImpact(self, address, timestamp):
        if not self.recordingSensor:
            self.lastImpactTime = timestamp

            # Start video recording
            self.recordingSensor = address
            self.fileprefix = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            asyncio.get_running_loop().create_task(self.cam.record(f"{self.fileprefix}.mp4"))

            # Start new subtitle capture
            if self.subsEnabled:
                self.subs = Subtitler(f"{self.fileprefix}.srt")

        elif self.recordingSensor and address in self.recordingSensor:
            self.lastImpactTime = timestamp
            impactTime = self.cam.getCurrentTimestamp()
            print(f"impactTime: {impactTime}")
            self.tof += 1
            if self.subsEnabled:
                self.subs.append(self.departTime, impactTime, (self.lastImpactTime - self.lastDepartTime) / 1000)

    def onDepart(self, address, timestamp):
        if self.recordingSensor and address in self.recordingSensor:
            self.lastDepartTime = timestamp
            if self.recordingSensor:
                self.departTime = self.cam.getCurrentTimestamp()
                print(f"departTime: {self.departTime}")

    def onIdle(self, address, timestamp):
        if self.recordingSensor and address in self.recordingSensor:
            self.lastIdleTime = timestamp
            self.lastDepartTime = 0
            self.lastImpactTime = 0
            self.recordingSensor = None
            self.departTime = 0

            syncError = self.cam.stop()
            print(f"Sync Error: {syncError}")

            if self.subsEnabled:
                self.subs.timeshift(syncError)
                self.subs.save()

            asyncio.get_running_loop().create_task(self.cam.play(f"{self.fileprefix}.mp4"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-i", "--ip", required=True, type=str, help="The IP address of the Veriflite portal")
    parser.add_argument("-v", "--device", type=str, default="/dev/video0", help="The video device to use (defaults to /dev/video0)")
    parser.add_argument("-d", "--delay", type=int, default=5, help="Delay after recording before video (deafults to 5 seconds)")
    parser.add_argument("-f", "--fps", type=int, default=30, help="Frame rate to record from the webcam")
    parser.add_argument("-r", "--res", type=str, default="1920x1080", help="Video resolution to record from the webcam")
    parser.add_argument("-s", "--subs", action='store_true', help="Create .subs file")
    args = parser.parse_args()

    fliteSight = Flite_Sight(f"ws://{args.ip}:4651/raw", args.device, args.res, args.fps, args.delay, args.subs)
    while True:
        asyncio.run(fliteSight.start())

