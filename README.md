# v4l2-stream-python

## Quick Start

```
$ git clone https://github.com/mbyzhang/v4l2-stream-python
$ cd v4l2-stream-python
$ sudo apt install python3 python3-pip python3-yaml gstreamer1.0-libav gstreamer1.0-plugins-bad gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-ugly gstreamer1.0-tools gstreamer1.0-vaapi gir1.2-gstreamer-1.0
$ pip3 install -r requirements.txt
$ python3 main.py
```

and in another Terminal

```
$ cd v4l2-stream-python/jsmpeg
$ python3 -m http.server
```

and open http://localhost:8000/view-stream.html in your browser.
