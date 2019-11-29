#!/usr/bin/python3

import threading
import time
import base64
import sys, os
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GObject, GLib
import json
import signal
import asyncio
import websockets
import logging
import janus
import argparse
import yaml

class GstPipeline():
    def __init__(self, frame_queue, pipeline_options):
        self.pipeline        = None
        self.mainloop        = None
        self.frame_queue     = frame_queue
        self.pipeline_options  = pipeline_options

    def pull_frame(self):
        sample = self.videosink.emit("pull-sample")
        if sample is not None:
            self.current_buffer = sample.get_buffer()
            current_data = self.current_buffer.extract_dup(0, self.current_buffer.get_size())
            logging.info(".")
            try:
                #   logging.info("Frame put into the queue, current size is {}".format(self.frame_queue.qsize()))
                self.frame_queue.put(current_data)
            except asyncio.QueueFull:
                logging.warning("Frame queue is full, dropping the frame")
        return False

    def gst_thread(self):
        Gst.init(None)

        source_options = self.pipeline_options.source
        encoder_options = self.pipeline_options.encoder
        decoder_options = self.pipeline_options.decoder
        sink_options = self.pipeline_options.sink

        if source_options.type == "v4l2":
            cmd_source = "v4l2src name=src do-timestamp=1 ! name=cf caps=image/jpeg,width=1280,height=720,framerate=30/1 ! vaapijpegdec ! "
        elif source_options.type == "ws":
            cmd_source = "appsrc name=src do-timestamp=1 ! "
        else:
            raise Exception("Unsupported source type {}".format(source_options.type))
                
        if decoder_options.type == "none":
            cmd_decoder = ""
        elif decoder_options.type == "h265":
            cmd_decoder = "h265parse ! avdec_h265 ! "
        elif decoder_options.type == "h264":
            cmd_decoder = "h264parse ! avdec_h264 ! "
        else:
            raise Exception("Unsupported decoder type {}".format(decoder_options.type))

        if encoder_options.type == "h265":
            cmd_encoder = "vaapih265enc name=encoder ! "
        elif encoder_options.type == "h264":
            cmd_encoder = "vaapih264enc name=encoder ! "
        elif encoder_options.type == "h264cb":
            cmd_encoder = "vaapih264enc name=encoder ! video/x-h264,stream-format=byte-stream,alignment=au,profile=constrained-baseline ! "
        elif encoder_options.type == "mpeg1sw":
            cmd_encoder = "avenc_mpeg1video name=encoder ! mpegvideoparse ! mpegtsmux name=mpegtsmux ! "
        else:
            raise Exception("Unsupported encoder type {}".format(encoder_options.type))

        if sink_options.type == "ws":
            cmd_sink = "appsink name=sink"
        elif sink_options.type == "fake":
            cmd_sink = "fakesink"
        else:
            raise Exception("Unsupported sink type {}".format(sink_options.type))
        
        self.pipeline = Gst.parse_launch(cmd_source + cmd_decoder + cmd_encoder + cmd_sink)

        self.videosource = self.pipeline.get_by_name("src")
        if source_options.type == "v4l2":
            if source_options.device is not None:
                self.videosource.set_property("device", source_options.device)
            
            if source_options.width is not None and source_options.height is not None and source_options.fps is not None:
                width = source_options.width
                height = source_options.height
                fps = source_options.fps
                if type(width) is not int or type(height) is not int or type(fps) is not int:
                    raise TypeError("V4L2 source height, width, and fps must be int")
                self.pipeline.get_by_name("cf").set_property("caps", Gst.Caps.from_string("image/jpeg,width={},height={},framerate={}/1", width, height, fps))
        #elif source_options.type == "ws":

            
        if sink_options.type == "ws":
            self.videosink = self.pipeline.get_by_name("sink")
            self.videosink.set_property("drop", True)
            self.videosink.set_property("max-buffers", 3)
            self.videosink.set_property("emit-signals", True)
            self.videosink.set_property("sync", False)
            self.videosink.connect("new-sample", self.pull_frame)
        
        if encoder_options.bitrate is not None:
            bitrate = encoder_options.bitrate # in kbps for h264 and h265
            if encoder_options.type == "mpeg1":
                bitrate = encoder_options.bitrate * 1000 # in bps for mpeg1
            self.pipeline.get_by_name("encoder").set_property("bitrate", bitrate)
        
        if encoder_options.type == "mpeg1":
            self.pipeline.get_by_name("mpegtsmux").set_property("alignment", encoder_options.tsmux_alignment)


        self.pipeline.set_state(Gst.State.PAUSED)

        self.mainloop = GLib.MainLoop()
        self.mainloop.run()
    
    def set_playing(self):
        logging.info("Pipeline state is set to PLAYING")
        self.pipeline.set_state(Gst.State.PLAYING)
    
    def set_paused(self):
        logging.info("Pipeline state is set to PAUSED")
        self.pipeline.set_state(Gst.State.PAUSED)


class WebSocketBroadcastServer():
    def __init__(self, port=8765, host='0.0.0.0'):
        self.clients = set()
        self.host = host
        self.port = port
        self.queue = None
        self.on_first_open = None
        self.on_last_closed = None
        self.loop = asyncio.get_event_loop()
    
    def bind(self, on_first_open, on_last_closed):
        self.on_first_open = on_first_open
        self.on_last_closed = on_last_closed
    
    def set_queue(self, queue):
        self.queue = queue
    
    def run(self):
        self.loop.create_task(self.broadcast())
        self.loop.run_until_complete(websockets.serve(self.handler, self.host, self.port, subprotocols=["JsMpeg"]))
        self.loop.run_forever()

    async def broadcast(self):
        while True:
            message = await self.queue.get()
            await asyncio.gather(
                *[ws.send(message) for ws in self.clients],
                return_exceptions=False,
            )

    async def handler(self, websocket, path):
        if len(self.clients) == 0 and self.on_first_open is not None:
            logging.info("Streaming started")
            self.on_first_open()
        self.clients.add(websocket)
        try:
            async for msg in websocket:
                pass
        except websockets.ConnectionClosedError:
            logging.info("Connection closed")
        finally:
            self.clients.remove(websocket)
            if len(self.clients) == 0 and self.on_last_closed is not None:
                logging.info("Streaming stopped")
                self.on_last_closed()

class WebSocketClient():
    def __init__(self, uri='ws://127.0.0.1:8765'):
        self.uri = uri

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--config", dest="config", default="config.yaml", help="config file to use")
    parser.add_argument("-v", "--verbose", dest="verbose", help="be verbose", action='store_true')
    args = parser.parse_args()

    with open(args.config) as f:
        config = yaml.load(f, Loader=yaml.FullLoader)
    
    logging_level = "INFO"
    if args.verbose:
        logging_level = "DEBUG"
    logging.basicConfig(level=logging_level)

    if "streams" not in config:
        raise Exception("You must specify streams in the config")

    for stream in config["streams"]:
        logging.info(stream)
    '''
    server = WebSocketBroadcastServer()
    frame_queue = janus.Queue(maxsize=100, loop=server.loop)
    server.set_queue(frame_queue.async_q)
    webcam = GstPipeline(frame_queue.sync_q)
    server.bind(webcam.set_playing, webcam.set_paused)
    webcam_thread = threading.Thread(target=webcam.gst_thread)
    webcam_thread.start()
    server.run()
    '''

    