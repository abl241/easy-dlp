import yt_dlp
import sys
#import os
#import easygui

URLS = sys.argv[1]
location = sys.argv[2]

ydl_opts = {
    'cookiefile': 'cookies.txt',
    'format': 'bv*[vcodec!^=vp9]+ba[ext=m4a]/b[ext=mp4]/b',  # Ensure MP4 is preferred
    'merge_output_format': 'mp4',   # Force output as MP4
    'outtmpl': location+'/%(title)s.%(ext)s',
    'add-metadata': True,           # Add metadata to the video
    'ffmpeg-location': r"ffmpeg", #/Library/Frameworks/Python.framework/Versions/3.12/bin/
    'ignoreerrors': True,           # Skip broken videos
    'retries': 3,                   # Retry failed downloads
    'timeout': 60,
    'writethumbnail': True,
    'embedthumbnail': True,
    'postprocessors': [
        {
            'key': 'FFmpegVideoRemuxer',  # Ensure MP4 compatibility by remuxing
            'preferedformat': 'mp4',
        },
        {
            'key': 'FFmpegVideoConvertor',  # Convert the video format
            'preferedformat': 'mp4',        # Ensure output format is MP4
        },
        {
        'key': 'EmbedThumbnail',
    },],
    'postprocessor_args': [
        '-vcodec', 'libx264',  # Use H.264 video codec
        '-acodec', 'aac',      # Use AAC audio codec
        '-preset', 'medium',   # Set encoding speed/quality balance
        #'-crf', '23',          # Video quality control (lower means better quality)
        #'-movflags', '+faststart'  # Optimize file for web playback and editing
    ],
}

with yt_dlp.YoutubeDL(ydl_opts) as ydl:
    error_code = ydl.download(URLS)