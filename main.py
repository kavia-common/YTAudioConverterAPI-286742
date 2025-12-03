"""Flask-based YouTube Audio Converter API.

This service exposes endpoints to search YouTube videos, download/convert audio to MP3,
serve audio files with HTTP range support, and automatically clean up expired files.

Endpoints:
- GET /                -> Returns basic usage information.
- GET /search?q=...    -> Searches for short YouTube videos (< 5 minutes).
- GET /download?video_url=... -> Downloads audio for a given YouTube video and returns a JSON streaming response with the direct MP3 link.
- GET /audios/<filename> -> Streams previously generated MP3 files with range support.

Note: FFmpeg must be available in the environment for pydub/youtube_dl conversions.
"""

from flask import Flask, request, jsonify, Response, stream_with_context, make_response
from youtubesearchpython import VideosSearch
import os
import re
import time
from flask_cors import CORS
import json
from threading import Thread
import threading
import youtube_dl
from pydub import AudioSegment
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# Ensure required runtime directory exists to avoid runtime errors
AUDIO_DIR = "audios"
os.makedirs(AUDIO_DIR, exist_ok=True)

app = Flask(__name__)
cors = CORS(app)

# Define the retention period in seconds (default: 2 hours)
RETENTION_PERIOD = 2 * 60 * 60

# Configure rate limiting (memory backend)
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["30 per second"],
    storage_uri="memory://",
)

# PUBLIC_INTERFACE
@app.route("/")
def nothing():
    """Root endpoint.
    Returns a short usage message instructing the client to use /download or /audios/<filename>.

    Returns:
        flask.Response: JSON message with usage guidance.
    """
    response = jsonify({"msg": "Use /search?q=..., /download?video_url=..., or /audios/<filename>"})
    response.headers.add("Content-Type", "application/json")
    return response


def compress_audio(file_path: str) -> None:
    """Re-encode the audio file as MP3 at 256 kbps.

    Args:
        file_path (str): Full file path to the audio file to compress.
    """
    audio = AudioSegment.from_file(file_path)
    compressed_audio = audio.export(file_path, format="mp3", bitrate="256k")
    compressed_audio.close()


def generate(host_url: str, video_url: str):
    """Generator that downloads/converts audio and yields a JSON response as bytes.

    Args:
        host_url (str): The request host URL (e.g., http://127.0.0.1:5000/)
        video_url (str): The YouTube video URL to download.

    Yields:
        bytes: A JSON payload encoded to bytes describing the direct MP3 link (or an error).
    """
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": f"{AUDIO_DIR}/%(id)s.%(ext)s",
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "256",
                "nopostoverwrites": True,
            }
        ],
        "verbose": True,
    }

    with youtube_dl.YoutubeDL(ydl_opts) as ydl:
        info_dict = ydl.extract_info(video_url, download=False)
        duration = info_dict.get("duration")

        if duration and duration <= 300:
            info_dict = ydl.extract_info(video_url, download=True)
            audio_file_path = ydl.prepare_filename(info_dict)
            thumbnail_url = info_dict.get("thumbnail")

            file_name, file_extension = os.path.splitext(audio_file_path)
            file_name = os.path.basename(file_name)
            expiration_timestamp = int(time.time()) + RETENTION_PERIOD

            # Ensure final MP3 exists and is compressed to target bitrate
            mp3_path = os.path.join(AUDIO_DIR, file_name + ".mp3")
            compress_audio(mp3_path)

            # Use host_url to build a correct, self-hosted direct link
            direct_link = f"{host_url.rstrip('/')}/audios/{file_name}.mp3"

            response_dict = {
                "img": thumbnail_url,
                "direct_link": direct_link,
                "expiration_timestamp": expiration_timestamp,
            }
            response_json = json.dumps(response_dict)
            response_bytes = response_json.encode("utf-8")
            with app.app_context():
                yield response_bytes
        else:
            response_dict = {"error": "Video duration must be less than or equal to 5 minutes."}
            response_json = json.dumps(response_dict)
            response_bytes = response_json.encode("utf-8")
            yield response_bytes


# PUBLIC_INTERFACE
@app.route("/search", methods=["GET"])
@limiter.limit("5/minute", error_message="Too many requests")
def search():
    """Search YouTube for short videos under 5 minutes.

    Query Parameters:
        q (str): The search query.

    Returns:
        flask.Response: JSON object with a 'search' list of results (title, url, thumbnail).
    """
    q = request.args.get("q")
    if q is None or len(q) == 0:
        return jsonify({"error": "Invalid search query"}), 400

    s = VideosSearch(q, limit=15)
    results = s.result()["result"]
    search_results = []
    for video in results:
        duration = video.get("duration") or ""
        if ":" in duration:
            parts = duration.split(":")
            if len(parts) == 2:  # Minutes:Seconds format
                try:
                    minutes, seconds = map(int, parts)
                    total_seconds = minutes * 60 + seconds
                    if total_seconds < 300:  # Less than 5 minutes
                        search_results.append(
                            {
                                "title": video.get("title"),
                                "url": video.get("link"),
                                "thumbnail": (video.get("thumbnails") or [{}])[0].get("url"),
                            }
                        )
                except ValueError:
                    # Skip malformed durations gracefully
                    continue

    response = jsonify({"search": search_results})
    response.headers.add("Content-Type", "application/json")
    return response


# PUBLIC_INTERFACE
@app.route("/download", methods=["GET"])
@limiter.limit("5/minute", error_message="Too many requests")  # Limit to 5 requests per minute
def download_audio():
    """Download and convert a YouTube video's audio to MP3.

    Query Parameters:
        video_url (str): Full YouTube video URL.

    Returns:
        flask.Response: Streaming JSON response describing the generated MP3 direct link,
                        or an error JSON payload if invalid.
    """
    video_url = request.args.get("video_url")
    if not video_url:
        return jsonify({"error": "Missing required parameter: video_url"}), 400

    # Use the host URL (e.g., http://127.0.0.1:5000/) to build direct links locally
    host_url = request.host_url
    return Response(stream_with_context(generate(host_url, video_url)), mimetype="application/json")


# PUBLIC_INTERFACE
@app.route("/audios/<path:filename>", methods=["GET"])
@limiter.limit("2/5seconds", error_message="Too many requests")  # Limit to 2 requests per 5 seconds
def serve_audio(filename: str):
    """Serve a generated MP3 file with HTTP range support for streaming clients.

    Path Parameters:
        filename (str): The MP3 filename within the audios directory.

    Returns:
        flask.Response: A 206 Partial Content or 200 OK response with appropriate headers.
    """
    root_dir = os.getcwd()
    file_path = os.path.join(root_dir, AUDIO_DIR, filename)

    # Check if the file exists
    if not os.path.isfile(file_path):
        return make_response("Audio file not found", 404)

    # Get the total file size
    file_size = os.path.getsize(file_path)

    # Parse the Range header
    range_header = request.headers.get("Range")

    if range_header:
        # Extract the start and end positions from the Range header
        start_pos, end_pos = parse_range_header(range_header, file_size)

        # Set the response headers for partial content
        response = make_partial_response(file_path, start_pos, end_pos, file_size)
    else:
        # Set the response headers for the entire content
        response = make_entire_response(file_path, file_size)

    # Set CORS and content headers
    response.headers.set("Access-Control-Allow-Origin", "*")
    response.headers.set("Access-Control-Allow-Methods", "GET")
    response.headers.set("Content-Type", "audio/mpeg")

    return response


def parse_range_header(range_header: str, file_size: int):
    """Parse an HTTP Range header and return byte start/end positions.

    Args:
        range_header (str): The value of the Range header.
        file_size (int): The total size of the file in bytes.

    Returns:
        tuple[int, int]: Start and end byte positions.
    """
    range_match = re.search(r"(\\d+)-(\\d*)", range_header)
    start_pos = int(range_match.group(1)) if range_match and range_match.group(1) else 0
    end_pos = int(range_match.group(2)) if range_match and range_match.group(2) else file_size - 1

    # Clamp to bounds
    start_pos = max(0, min(start_pos, file_size - 1))
    end_pos = max(start_pos, min(end_pos, file_size - 1))

    return start_pos, end_pos


def make_partial_response(file_path: str, start_pos: int, end_pos: int, file_size: int):
    """Build a 206 Partial Content response for a byte range request."""
    with open(file_path, "rb") as file:
        file.seek(start_pos)
        content_length = end_pos - start_pos + 1
        content = file.read(content_length)

    response = make_response(content)

    response.headers.set("Content-Range", f"bytes {start_pos}-{end_pos}/{file_size}")
    response.headers.set("Content-Length", str(content_length))
    response.status_code = 206

    return response


def make_entire_response(file_path: str, file_size: int):
    """Build a 200 OK response for full-file download."""
    with open(file_path, "rb") as file:
        content = file.read()

    response = make_response(content)
    response.headers.set("Content-Length", str(file_size))

    return response


def delete_expired_files() -> None:
    """Delete files in AUDIO_DIR that exceed the retention period."""
    current_timestamp = int(time.time())

    # Iterate over the files in the 'audios' directory
    for file_name in os.listdir(AUDIO_DIR):
        file_path = os.path.join(AUDIO_DIR, file_name)
        if (
            os.path.isfile(file_path)
            and current_timestamp > os.path.getmtime(file_path) + RETENTION_PERIOD
        ):
            # Delete the expired file
            try:
                os.remove(file_path)
            except OSError:
                # Ignore failures to remove individual files
                pass


def delete_files_task():
    """Schedule periodic deletion of expired files."""
    delete_expired_files()
    threading.Timer(100, delete_files_task).start()


def run():
    """Run the Flask development server."""
    app.run(host="0.0.0.0")


def keep_alive():
    """Start the Flask server in a background thread."""
    t = Thread(target=run)
    t.start()


if __name__ == "__main__":
    # Make sure the audio output directory exists before serving requests
    os.makedirs(AUDIO_DIR, exist_ok=True)

    # Schedule a task to delete expired files periodically
    delete_files_task()

    # Start the app
    keep_alive()
