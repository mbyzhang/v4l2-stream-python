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
import traceback

class GstPipeline():
    def __init__(self, pipeline_options):
        self.pipeline        = None
        self.mainloop        = None
        self.videosource     = None
        self.videosink       = None
        self.on_pipeline_ready = None
        self.frame_queue     = pipeline_options["sink"]["frame_queue"]
        self.pipeline_options  = pipeline_options

    def pull_frame(self, data):
        sample = self.videosink.emit("pull-sample")
        if sample is not None:
            self.current_buffer = sample.get_buffer()
            current_data = self.current_buffer.extract_dup(0, self.current_buffer.get_size())
            #logging.info(".")
            try:
                #   logging.info("Frame put into the queue, current size is {}".format(self.frame_queue.qsize()))
                self.frame_queue.put_nowait(current_data)
            except janus.SyncQueueFull:
                logging.warning("Buffer overrun")
        return False
    
    def push_frame(self, buf):
       if self.videosource is not None:
            self.videosource.emit('push-buffer', Gst.Buffer.new_wrapped(buf))

    def gst_thread(self):
        Gst.init(None)

        source_options = self.pipeline_options["source"]
        encoder_options = self.pipeline_options["encoder"]
        decoder_options = self.pipeline_options["decoder"]
        sink_options = self.pipeline_options["sink"]

        try:
            if source_options["type"] == "v4l2":
                cmd_source = "v4l2src name=src do-timestamp=1 ! capsfilter name=cf caps=image/jpeg,width=1280,height=720,framerate=30/1 ! vaapijpegdec ! "
            elif source_options["type"] == "ws":
                cmd_source = "appsrc name=src do-timestamp=1 is-live=1 ! "
            else:
                raise Exception("Unsupported source type {}".format(source_options["type"]))
                    
            if decoder_options["type"] == "none":
                cmd_decoder = ""
            elif decoder_options["type"] == "h265":
                cmd_decoder = "h265parse ! avdec_h265 ! "
            elif decoder_options["type"] == "h264":
                cmd_decoder = "h264parse ! avdec_h264 ! "
            else:
                raise Exception("Unsupported decoder type {}".format(decoder_options["type"]))

            if encoder_options["type"] == "h265":
                cmd_encoder = "vaapih265enc name=encoder ! "
            elif encoder_options["type"] == "h264":
                cmd_encoder = "vaapih264enc name=encoder ! "
            elif encoder_options["type"] == "h264cb":
                cmd_encoder = "vaapih264enc name=encoder ! video/x-h264,stream-format=byte-stream,alignment=au,profile=constrained-baseline ! "
            elif encoder_options["type"] == "mpeg1sw":
                cmd_encoder = "avenc_mpeg1video name=encoder ! mpegvideoparse ! mpegtsmux name=mpegtsmux ! "
            else:
                raise Exception("Unsupported encoder type {}".format(encoder_options["type"]))

            if sink_options["type"] == "ws":
                cmd_sink = "appsink name=sink"
            elif sink_options["type"] == "fake":
                cmd_sink = "fakesink"
            else:
                raise Exception("Unsupported sink type {}".format(sink_options["type"]))

            cmd = cmd_source + cmd_decoder + cmd_encoder + cmd_sink
            logging.info("Starting pipeline {}".format(cmd))
            self.pipeline = Gst.parse_launch(cmd)

            self.videosource = self.pipeline.get_by_name("src")

            if source_options["type"] == "v4l2":
                if source_options["device"] is not None:
                    self.videosource.set_property("device", source_options["device"])
                
                if source_options["width"] is not None and source_options["height"] is not None and source_options["fps"] is not None:
                    width = source_options["width"]
                    height = source_options["height"]
                    fps = source_options["fps"]
                    if type(width) is not int or type(height) is not int or type(fps) is not int:
                        raise TypeError("V4L2 source height, width, and fps must be int")
                    self.pipeline.get_by_name("cf").set_property("caps", Gst.Caps.from_string("image/jpeg,width={},height={},framerate={}/1".format(width, height, fps)))
                
            if sink_options["type"] == "ws":
                self.videosink = self.pipeline.get_by_name("sink")
                self.videosink.set_property("drop", True)
                self.videosink.set_property("max-buffers", 2)
                self.videosink.set_property("emit-signals", True)
                self.videosink.set_property("sync", False)
                self.videosink.connect("new-sample", self.pull_frame)
            
            
            if encoder_options["bitrate"] is not None:
                bitrate = encoder_options["bitrate"] # in kbps for h264 and h265
                if encoder_options["type"] == "mpeg1sw":
                    bitrate *= 1000 # in bps for mpeg1
                self.pipeline.get_by_name("encoder").set_property("bitrate", bitrate)
            
            if encoder_options["type"] == "mpeg1sw":
                self.pipeline.get_by_name("mpegtsmux").set_property("alignment", encoder_options["tsmux_alignment"])
        
        except KeyError as e:
            raise Exception("You must specify {} in the options".format(e))

        self.pipeline.set_state(Gst.State.PAUSED)
        
        if self.on_pipeline_ready is not None:
            self.on_pipeline_ready()
        
        self.mainloop = GLib.MainLoop()
        self.mainloop.run()

    def set_playing(self):
        logging.info("Pipeline state is set to PLAYING")
        self.pipeline.set_state(Gst.State.PLAYING)
    
    def set_paused(self):
        logging.info("Pipeline state is set to PAUSED")
        self.pipeline.set_state(Gst.State.PAUSED)


class WebSocketBroadcastServer():
    def __init__(self, port=8765, host='0.0.0.0', loop=None):
        self.clients = set()
        self.host = host
        self.port = port
        self.queue = None
        self.on_first_open = None
        self.on_last_closed = None
        if loop is None:
            self.loop = asyncio.get_event_loop()
        else:
            self.loop = loop
    
    def set_queue(self, queue):
        self.queue = queue
    
    def run(self):
        self.loop.create_task(self.broadcast())
        self.loop.run_until_complete(websockets.serve(self.handler, self.host, self.port))

    async def broadcast(self):
        while True:
            message = await self.queue.get()
            await asyncio.gather(
                *[ws.send(message) for ws in self.clients],
                return_exceptions=False,
            )

    async def handler(self, websocket, path):
        if len(self.clients) == 0 and self.on_first_open is not None:
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
                self.on_last_closed()

class WebSocketClient():
    def __init__(self, uri, on_message):
        self.uri = uri
        self.on_message = on_message
        self.loop = asyncio.get_event_loop()
        self.on_open = None
        self.on_closed = None
        
    def run(self):
        logging.info("Client starting")
        
        self.loop.call_soon_threadsafe(self.loop.create_task(self.pull()))

    async def pull(self):
        logging.info("Start pulling")
        try:
            async with websockets.connect(self.uri) as websocket:
                if self.on_open is not None:
                    self.on_open()
                
                while True:
                    message = await websocket.recv()
                    if self.on_message is not None:
                        self.on_message(message)
        finally:
            if self.on_closed is not None:
                self.on_closed()


if __name__ == "__main__":
    try:
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

        streams = []
        for pipeline_options in config["streams"]:
            try:
                source_options = pipeline_options["source"]
                sink_options = pipeline_options["sink"]

                if sink_options["type"] == "ws":
                    server = WebSocketBroadcastServer(sink_options["port"], sink_options["host"])
                    frame_queue = janus.Queue(maxsize=sink_options["queue_size"], loop=server.loop)
                    server.set_queue(frame_queue.async_q)
                    pipeline_options["sink"]["frame_queue"] = frame_queue.sync_q
                    server.run()

                pipeline = GstPipeline(pipeline_options)

                if sink_options["type"] == "ws":
                    server.on_first_open = pipeline.set_playing
                    server.on_last_closed = pipeline.set_paused
                
                if source_options["type"] == "ws":
                    if "uri" not in source_options:
                        raise Exception("You must specify URI in source options when using WebSocket")
                    client = WebSocketClient(source_options["uri"], pipeline.push_frame)
                    client.on_open = pipeline.set_playing
                    client.on_closed = pipeline.set_paused
                    pipeline.on_pipeline_ready = client.run
                    #client.run()
                
                pipeline_thread = threading.Thread(target=pipeline.gst_thread)
                
                pipeline_thread.start()

                streams.append({
                    "pipeline": pipeline,
                    "pipeline_thread": pipeline_thread
                })
                
            except KeyError as e:
                raise Exception("You must specify {} in the options".format(e))
        
        asyncio.get_event_loop().run_forever()

    except KeyboardInterrupt:
        logging.info("Exiting...")
        
        loop = asyncio.get_event_loop()
        # Handle shutdown gracefully by waiting for all tasks to be cancelled
        tasks = asyncio.gather(*asyncio.Task.all_tasks(loop=loop), loop=loop, return_exceptions=True)
        tasks.add_done_callback(lambda t: loop.stop())
        tasks.cancel()

        # Keep the event loop running until it is either destroyed or all
        # tasks have really terminated
        while not tasks.done() and not loop.is_closed():
            loop.run_forever()
            
        for stream in streams:
            stream["pipeline"].mainloop.quit()
            stream["pipeline_thread"].join()


    except Exception as e:
        logging.error(e)
        #traceback.print_exc()

    