#!/usr/bin/python3

     #########  ##############  #######   #   ##         ##############  #########
    ##     ##  ##    ##    ##  ##        #   ##         ##    ##    ##  ##     ##
   ##     ##  ##    ##    ##  #######   #   #########  ##    ##    ##  ##     ##
  ##     ##  ##    ##    ##       ##   #   ##     ##  ##    ##    ##  ##     ##
 ##     ##  ##    ##    ##  #######   #   #########  ##    ##    ##  ##     ##
 
# SRT Subtitles to Teletext converter
# Combined OBS Studio / OMXPlayer Version

from subprocess import Popen, PIPE
from datetime import datetime, timedelta
import bs4
import getpass
import os
import sys
import time
import re
import json
import chardet

import webvtt

import interface
from legaliser import charsub

#Convert ASS time to SRT time
def ass_time_to_srt(t):
    h, m, rest = t.strip().split(':')
    if '.' in rest:
        s, cs = rest.split('.')
        ms = int(cs) * 10
    else:
        s, ms = rest, 0
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{ms:03d}"

def ass_strip_tags(text):
    text = re.sub(r'\{[^}]*\}', '', text)
    text = text.replace('\\N', '\n').replace('\\n', '\n').replace('\\h', ' ')
    return text.strip()

def convert_ass_to_srt(ass_path):
    with open(ass_path, 'r', encoding='utf-8', errors='replace') as f:
        raw = f.read()
    raw = raw.lstrip('\ufeff').replace('\r\n', '\n').replace('\r', '\n')

    dialogue_re = re.compile(
        r'^Dialogue:\s*\d+,'
        r'(\d+:\d{2}:\d{2}\.\d+),'
        r'(\d+:\d{2}:\d{2}\.\d+),'
        r'[^,]*,[^,]*,'
        r'\d+,\d+,\d+,'
        r'[^,]*,'
        r'(.*)$',
        re.MULTILINE
    )

    entries = []
    for m in dialogue_re.finditer(raw):
        start = ass_time_to_srt(m.group(1))
        end   = ass_time_to_srt(m.group(2))
        text  = ass_strip_tags(m.group(3))
        if text:
            entries.append((start, end, text))

    entries.sort(key=lambda e: e[0])

    srt_path = ass_path + '.converted.srt'
    with open(srt_path, 'w', encoding='utf-8') as f:
        for i, (start, end, text) in enumerate(entries, 1):
            f.write(f"{i}\n{start} --> {end}\n{text}\n\n")

    print(f"Converted ASS to SRT: {srt_path} ({len(entries)} subtitles)")
    return srt_path

def ensure_srt(subtitle_file):
    with open(subtitle_file, 'rb') as f:
        header = f.read(20).lstrip(b'\xef\xbb\xbf')
    if header.startswith(b'[Script Info]'):
        print("File is ASS/SSA format, converting to SRT...")
        return convert_ass_to_srt(subtitle_file)
    return subtitle_file

def show_status_message(c, message):
    MAX_CHARS = 35
    MAX_ROWS  = 3
    FIRST_ROW = 23  # start at the bottom, move up if more lines needed

    # Word-wrap the message
    words = message.split()
    lines = []
    current = ""
    for word in words:
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= MAX_CHARS:
            current += " " + word
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)

    # Truncate to max rows
    lines = lines[:MAX_ROWS]

    # Calculate starting row so text sits at the bottom
    start_row = FIRST_ROW - (len(lines) - 1) * 2  # rows 19/21/23 (every 2 rows = double height)

    # Clear all three possible message rows first
    for row in [19, 21, 23]:
        try:
            c.deleteRow(row)
        except Exception:
            pass

    for i, line in enumerate(lines):
        row = start_row + i * 2
        # Build exactly 40 bytes: 2 prefix + up to 35 text + 3 suffix, padded to 40
        payload = f"\x0b\x0b{line}\x0a\x0a "
        encoded = payload.encode(encoding='ascii', errors='replace')
        # Pad or trim to exactly 40 bytes
        encoded = encoded[:40].ljust(40, b' ')
        c.setRow(row, encoded)


# Try to import OBS websocket
try:
    from obswebsocket import obsws, requests as obs_requests
    OBS_AVAILABLE = True
except ImportError:
    OBS_AVAILABLE = False
    print("Warning: obswebsocket not available, OBS mode disabled")

# Try to import dbus
try:
    import dbus
    DBUS_AVAILABLE = True
except ImportError:
    DBUS_AVAILABLE = False
    print("Warning: dbus not available, omxplayer mode disabled")

def load_config():
    default_config = {
        "mode": "omxplayer",
        "teletext_host": "172.20.64.33",
        "teletext_port": 19601,
        "obs_host": "localhost",
        "obs_port": 5555,
        "obs_password": "",
        "obs_source_name": "VT 1",
        "off_air_message": "NMP Television is currently off air",
        "subtitles_unavailable_message": "Subtitles are currently unavailable",
        "subtitle_delay": 0.0
    }
    
    config_file = "config.json"
    
    if os.path.exists(config_file):
        try:
            with open(config_file, 'r') as f:
                user_config = json.load(f)
                default_config.update(user_config)
                print(f"Loaded configuration from {config_file}")
        except Exception as e:
            print(f"Error loading config file: {e}")
            print("Using default configuration")
    else:
        # Create default config file
        try:
            with open(config_file, 'w') as f:
                json.dump(default_config, f, indent=4)
            print(f"Created default configuration file: {config_file}")
        except Exception as e:
            print(f"Could not create config file: {e}")
    
    return default_config

def detect_encoding(file_path):
    """
    Detect the encoding of a file using chardet.
    Falls back to common encodings if chardet fails.
    """
    try:
        with open(file_path, 'rb') as f:
            raw_data = f.read()
        
        result = chardet.detect(raw_data)
        encoding = result['encoding']
        confidence = result['confidence']
        
        print(f"Detected encoding: {encoding} (confidence: {confidence:.2%})")
        
        # If confidence is low, try common encodings
        if confidence < 0.7:
            for enc in ['utf-8', 'latin-1', 'cp1252', 'iso-8859-1']:
                try:
                    with open(file_path, 'r', encoding=enc) as f:
                        f.read()
                    print(f"Successfully validated encoding: {enc}")
                    return enc
                except (UnicodeDecodeError, LookupError):
                    continue
        
        return encoding
    except Exception as e:
        print(f"Error detecting encoding: {e}")
        # Default fallback order
        return 'latin-1'

def convert_to_utf8(file_path):
    """
    Convert a subtitle file to UTF-8 if it's not already.
    Creates a backup of the original file.
    """
    encoding = detect_encoding(file_path)
    
    if encoding and encoding.lower() not in ['utf-8', 'utf8']:
        print(f"Converting {file_path} from {encoding} to UTF-8...")
        
        # Create backup
        backup_path = file_path + '.bak'
        if not os.path.exists(backup_path):
            import shutil
            shutil.copy2(file_path, backup_path)
            print(f"Backup created: {backup_path}")
        
        # Read with detected encoding and write as UTF-8
        try:
            with open(file_path, 'r', encoding=encoding) as f:
                content = f.read()
            
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(content)
            
            print("Conversion successful!")
            return True
        except Exception as e:
            print(f"Error converting file: {e}")
            # Restore from backup if conversion failed
            if os.path.exists(backup_path):
                import shutil
                shutil.copy2(backup_path, file_path)
                print("Restored from backup")
            return False
    else:
        print(f"File is already UTF-8 or encoding is UTF-8")
        return True

def wrap_text(text, width=36):
    """
    Break text into lines of maximum 'width' characters.
    Tries to break on spaces where possible.
    """
    if len(text) <= width:
        return [text]
    
    lines = []
    remaining = text
    
    while len(remaining) > width:
        # Find last space within the limit
        break_point = remaining.rfind(' ', 0, width)
        
        if break_point == -1:
            # No space found, hard break
            break_point = width
        
        # Add the line
        lines.append(remaining[:break_point].rstrip())
        # Remove the added text (and space)
        remaining = remaining[break_point:].lstrip()
    
    # Add the last line
    if remaining:
        lines.append(remaining)
    
    return lines

def colourCode(colour):
    if colour.startswith("#"):
        colour = colour[0:7]
    
    textColourCode = {
        "black": chr(0),
        "red": chr(1),
        "#ff0000": chr(1),
        "green": chr(2),
        "#008000": chr(2),
        "#00ff00": chr(2),
        "yellow": chr(3),
        "#ffff00": chr(3),
        "blue": chr(4),
        "#0000ff": chr(4),
        "magenta": chr(5),
        "#ff00ff": chr(5),
        "cyan": chr(6),
        "#00ffff": chr(6),
        "white": chr(7),
        "#ffffff": chr(7),
    }
    
    if colour in textColourCode:
        return textColourCode[colour]
    else:
        return chr(7)

def send_subtitle(c, t, col, strings, clear, obs_client=None, source_name=None):
    # t is the time to keep subtitle on air
    # col is a teletext colour code
    # strings is a list of strings pre-wrapped to 36 characters wide
    # clear flag clears down page at end of subtitle
    now = time.time()
    
    # Retry logic for opening page
    max_retries = 5
    for attempt in range(max_retries):
        try:
            c.openPage(0x888, oneShot=True)
            break
        except:
            if attempt < max_retries - 1:
                time.sleep(0.1)
            else:
                raise
    
    c.setSubPage(0x0000)
    c.deleteRow(1)
    c.setSubPageOptions(erase=True, newsflash=False, subtitle=True, suppressHeader=False, 
                        update=False, interruptedSequence=False, inhibit=False, 
                        transmitPage=True, language=0, region=0, cycleTime=1, timedMode=False)

    row = 24 - (len(strings) * 2)

    # Ensure unused lines are clear
    if len(strings) < 3:
        c.deleteRow(18)
    if len(strings) < 2:
        c.deleteRow(20)

    for string in strings:
        line = bytearray(string.encode(encoding='latin-1', errors='replace') + bytearray([0x0a, 0x0a]))
        if len(line) < 40:
            line = line + bytearray([0x20] * (40 - len(line)))
        c.setRow(row, line[0:40])
        row = row + 2
    c.closePage()

    print("on:  ", time.time() - now)
    
    # Get current playhead position if OBS client is available
    if obs_client and source_name:
        try:
            status = obs_client.call(obs_requests.GetMediaInputStatus(inputName=source_name))
            current_play_head = status.getMediaCursor() / 1000.0
        except:
            pass
    
    # Calculate remaining time until midpoint based on elapsed time
    elapsed = time.time() - now
    sleep_time = max(0, (t / 2) - elapsed)
    time.sleep(sleep_time)
    
    # Re-transmit page halfway through on air time
    for attempt in range(max_retries):
        try:
            c.openPage(0x888, oneShot=True)
            break
        except:
            if attempt < max_retries - 1:
                time.sleep(0.1)
            else:
                raise
    
    c.setSubPage(0x0000)
    c.setSubPageOptions(erase=False, newsflash=False, subtitle=True, suppressHeader=False, 
                        update=False, interruptedSequence=False, inhibit=False, 
                        transmitPage=True, language=0, region=0, cycleTime=1, timedMode=False)
    c.closePage()
    print("re:  ", time.time() - now)
    
    # Get current playhead position again if OBS client is available
    if obs_client and source_name:
        try:
            status = obs_client.call(obs_requests.GetMediaInputStatus(inputName=source_name))
            current_play_head = status.getMediaCursor() / 1000.0
        except:
            pass
    
    # Calculate remaining time until end based on elapsed time
    elapsed = time.time() - now
    sleep_time = max(0, t - elapsed)
    time.sleep(sleep_time)
    
    if clear:
        # Clear down page at end of subtitle
        for attempt in range(max_retries):
            try:
                c.openPage(0x888, oneShot=True)
                break
            except:
                if attempt < max_retries - 1:
                    time.sleep(0.1)
                else:
                    raise
        
        c.setSubPage(0x0000)
        c.setSubPageOptions(erase=True, newsflash=False, subtitle=True, suppressHeader=False, 
                            update=False, interruptedSequence=False, inhibit=False, 
                            transmitPage=True, language=0, region=0, cycleTime=1, timedMode=False)
        c.deleteRow(18)
        c.deleteRow(20)
        c.deleteRow(22)
        c.closePage()
    print("off: ", time.time() - now)
    return time.time()

def transmit_subtitles_omx(c, subtitle_file, omxplayer, subtitle_delay=0.0):
    subtitle_file = ensure_srt(subtitle_file)
    subtitles = webvtt.from_srt(subtitle_file)
    dt_format = "%H:%M:%S.%f"
    
    if subtitle_delay != 0.0:
        print(f"Subtitle delay: {subtitle_delay:.2f}s")
    
    # Connect to VBIT2 instance
    c.setChannel(0)

    # Remove any existing page 888
    c.deletePage(0x888)
    c.openPage(0x888)
    c.setSubPage(0x0000)
    c.setSubPageOptions(erase=True, newsflash=False, subtitle=True, suppressHeader=False, 
                        update=False, interruptedSequence=False, inhibit=False, 
                        transmitPage=True, language=0, region=0, cycleTime=1, timedMode=False)
    c.closePage()
    
    for subtitle in subtitles:
        start_time_fixed = datetime.strptime(subtitle.start, dt_format) + timedelta(seconds=2208992400 + subtitle_delay)
        end_time_fixed = datetime.strptime(subtitle.end, dt_format) + timedelta(seconds=2208992400 + subtitle_delay)
        
        play_head = (omxplayer.position()/1000000)
        
        if (end_time_fixed.timestamp() < play_head):
            continue
        
        play_head = (omxplayer.position()/1000000)
        
        if (start_time_fixed.timestamp() - play_head) > 5:
            time.sleep(5)
        
        play_head = (omxplayer.position()/1000000)
        
        if (start_time_fixed.timestamp() - play_head) > 5:
            c.openPage(0x888)
            c.setSubPage(0x0000)
            c.setSubPageOptions(erase=False, newsflash=False, subtitle=True, suppressHeader=False, 
                                update=False, interruptedSequence=False, inhibit=False, 
                                transmitPage=True, language=0, region=0, cycleTime=1, timedMode=False)
            c.setRow(1, "\x04\x1d\x03                               \x0b\x0b888 ".encode(encoding='ascii'))
            c.closePage()
        
        if (start_time_fixed.timestamp() - play_head) > 0:
            time.sleep(start_time_fixed.timestamp() - play_head)
        
        teletextGroup = []
        
        for rawLine in subtitle.raw_text.splitlines():
            parsedLine = bs4.BeautifulSoup(rawLine, 'html.parser')
            
            # Collect all text without formatting codes first
            plainText = ""
            currentColor = chr(7)  # Start with white
            
            for chunk in parsedLine:
                if type(chunk) is bs4.element.Tag:
                    if chunk.has_attr('color'):
                        currentColor = colourCode(chunk['color'])
                        
                        if chunk['color'] == "red" and chunk.text == " ":
                            continue
                else:
                    currentColor = chr(7)
                
                captionText = chunk.text.strip()
                captionText = re.sub(r"\{\S*\}", "", captionText)
                captionText = charsub(captionText)
                
                plainText += captionText
            
            # Now break the text into chunks of 36 characters
            wrapped_lines = wrap_text(plainText, width=36)
            
            # Add each wrapped line with proper teletext codes
            for wrapped_line in wrapped_lines:
                teletextLine = ""
                
                # Add standard teletext prefix
                teletextLine += chr(0x0d)  # Double height
                teletextLine += chr(0x0b)  # Box start
                teletextLine += chr(0x0b)  # Box start
                teletextLine += currentColor
                teletextLine += wrapped_line
                
                teletextGroup.append(teletextLine)
                
                # Limit to 3 lines
                if len(teletextGroup) >= 3:
                    break
            
            # Limit to 3 lines for entire group
            if len(teletextGroup) >= 3:
                break
        
        # Ensure we don't have more than 3 lines
        teletextGroup = teletextGroup[:3]
        
        send_subtitle(c, end_time_fixed.timestamp() - (omxplayer.position()/1000000), 0x07, teletextGroup, True)
        
        print(f"Subtitle: {subtitle.start} - {subtitle.end} ({len(teletextGroup)} lines)")
        for i, line in enumerate(teletextGroup, 1):
            # Strip control characters voor display
            display_line = ''.join(char if ord(char) >= 32 else '' for char in line)
            print(f"  Line {i}: {display_line}")

def transmit_subtitles_obs(c, subtitle_file, obs_client, source_name, subtitle_delay=0.0):
    subtitle_file = ensure_srt(subtitle_file)
    subtitles = webvtt.from_srt(subtitle_file)
    dt_format = "%H:%M:%S.%f"
    
    if subtitle_delay != 0.0:
        print(f"Subtitle delay: {subtitle_delay:.2f}s")
    
    c.setChannel(0)

    c.deletePage(0x888)
    c.openPage(0x888)
    c.setSubPage(0x0000)
    c.setSubPageOptions(erase=True, newsflash=False, subtitle=True, suppressHeader=False, 
                        update=False, interruptedSequence=False, inhibit=False, 
                        transmitPage=True, language=0, region=0, cycleTime=1, timedMode=False)
    c.closePage()
    
    print("Waiting for media to start playing...")
    # Just return immediately if media is not playing - the monitor loop will show off-air
    status = obs_client.call(obs_requests.GetMediaInputStatus(inputName=source_name))
    play_head_ms = status.getMediaCursor()
    
    if play_head_ms is None:
        print("Media is not playing yet, returning to monitor loop")
        return
    
    print("Media is now playing, starting subtitle transmission")
    
    # Variables for stuck detection
    last_playhead_check = None
    playhead_stuck_since = None
    
    for subtitle in subtitles:
        start_dt = datetime.strptime(subtitle.start, dt_format)
        end_dt = datetime.strptime(subtitle.end, dt_format)
        
        start_seconds = start_dt.hour * 3600 + start_dt.minute * 60 + start_dt.second + start_dt.microsecond / 1000000.0 + subtitle_delay
        end_seconds = end_dt.hour * 3600 + end_dt.minute * 60 + end_dt.second + end_dt.microsecond / 1000000.0 + subtitle_delay
        
        status = obs_client.call(obs_requests.GetMediaInputStatus(inputName=source_name))
        play_head_ms = status.getMediaCursor()
        if play_head_ms is None:
            print("Media stopped playing")
            break
        play_head = play_head_ms / 1000.0
        
        # Check if playhead is stuck
        if last_playhead_check is not None and play_head_ms == last_playhead_check:
            if playhead_stuck_since is None:
                playhead_stuck_since = time.time()
            elif time.time() - playhead_stuck_since >= 5.0:
                print("Media playhead stuck for 5+ seconds - media has ended")
                break
        else:
            playhead_stuck_since = None
        
        last_playhead_check = play_head_ms
        
        if (end_seconds < play_head):
            continue
        
        status = obs_client.call(obs_requests.GetMediaInputStatus(inputName=source_name))
        play_head_ms = status.getMediaCursor()
        if play_head_ms is None:
            break
        play_head = play_head_ms / 1000.0
        
        if (start_seconds - play_head) > 5:
            time.sleep(5)
        
        status = obs_client.call(obs_requests.GetMediaInputStatus(inputName=source_name))
        play_head_ms = status.getMediaCursor()
        if play_head_ms is None:
            break
        play_head = play_head_ms / 1000.0
        
        if (start_seconds - play_head) > 5:
            c.openPage(0x888)
            c.setSubPage(0x0000)
            c.setSubPageOptions(erase=False, newsflash=False, subtitle=True, suppressHeader=False, 
                                update=False, interruptedSequence=False, inhibit=False, 
                                transmitPage=True, language=0, region=0, cycleTime=1, timedMode=False)
            c.setRow(1, "\x04\x1d\x03                               \x0b\x0b888 ".encode(encoding='ascii'))
            c.closePage()
        
        if (start_seconds - play_head) > 0:
            time.sleep(start_seconds - play_head)
        
        teletextGroup = []
        
        for rawLine in subtitle.raw_text.splitlines():
            parsedLine = bs4.BeautifulSoup(rawLine, 'html.parser')
            
            # Collect all text without formatting codes first
            plainText = ""
            currentColor = chr(7)  # Start with white
            
            for chunk in parsedLine:
                if type(chunk) is bs4.element.Tag:
                    if chunk.has_attr('color'):
                        currentColor = colourCode(chunk['color'])
                        
                        if chunk['color'] == "red" and chunk.text == " ":
                            continue
                else:
                    currentColor = chr(7)
                
                captionText = chunk.text.strip()
                captionText = re.sub(r"\{\S*\}", "", captionText)
                captionText = charsub(captionText)
                
                plainText += captionText
            
            # Now break the text into chunks of 36 characters
            wrapped_lines = wrap_text(plainText, width=36)
            
            # Add each wrapped line with proper teletext codes
            for wrapped_line in wrapped_lines:
                teletextLine = ""
                
                # Add standard teletext prefix
                teletextLine += chr(0x0d)  # Double height
                teletextLine += chr(0x0b)  # Box start
                teletextLine += chr(0x0b)  # Box start
                teletextLine += currentColor
                teletextLine += wrapped_line
                
                teletextGroup.append(teletextLine)
                
                # Limit to 3 lines
                if len(teletextGroup) >= 3:
                    break
            
            # Limit to 3 lines for entire group
            if len(teletextGroup) >= 3:
                break
        
        # Ensure we don't have more than 3 lines
        teletextGroup = teletextGroup[:3]
        
        # Get FRESH playhead position right before sending
        status = obs_client.call(obs_requests.GetMediaInputStatus(inputName=source_name))
        play_head_ms = status.getMediaCursor()
        if play_head_ms is None:
            break
        play_head = play_head_ms / 1000.0
        
        # Calculate duration based on current position
        duration = end_seconds - play_head
        
        send_subtitle(c, duration, 0x07, teletextGroup, True, obs_client, source_name)
        
        print(f"Subtitle: {start_seconds:.2f}s - {end_seconds:.2f}s ({len(teletextGroup)} lines) [played at {play_head:.2f}s, duration {duration:.2f}s]")
        for i, line in enumerate(teletextGroup, 1):
            # Strip control characters voor display
            display_line = ''.join(char if ord(char) >= 32 else '' for char in line)
            print(f"  Line {i}: {display_line}")

def monitor_obs_media(c, obs_client, source_name, config):
    """
    Continuously monitor OBS for media changes and transmit subtitles when available
    """
    current_media_file = None
    last_playhead = None
    playhead_stuck_time = None
    media_was_playing = False
    off_air_shown = False
    
    while True:
        try:
            # Get current media settings
            settings = obs_client.call(obs_requests.GetInputSettings(inputName=source_name))
            media_file = settings.getInputSettings().get('local_file', '')
            
            # Get playback status
            status = obs_client.call(obs_requests.GetMediaInputStatus(inputName=source_name))
            play_head_ms = status.getMediaCursor()
            
            # If no playhead (media not playing) and off-air not shown yet, show it
            if play_head_ms is None and not off_air_shown:
                print("No media playing - showing off air message")
                try:
                    c.setChannel(0)
                    c.deletePage(0x888)
                    c.openPage(0x888)
                    c.setSubPage(0x0000)
                    c.setSubPageOptions(erase=False, newsflash=False, subtitle=True, suppressHeader=False, 
                                        update=False, interruptedSequence=False, inhibit=False, 
                                        transmitPage=True, language=0, region=0, cycleTime=1, timedMode=False)
                    show_status_message(c, config['off_air_message'])
                    c.closePage()
                    off_air_shown = True
                except Exception as e:
                    print(f"Error showing off air message: {e}")
            
            # Check if playhead is stuck (hasn't moved for 5+ seconds)
            current_time = time.time()
            media_stuck = False
            
            if play_head_ms is not None and last_playhead is not None:
                if play_head_ms == last_playhead:
                    # Playhead hasn't moved
                    if playhead_stuck_time is None:
                        playhead_stuck_time = current_time
                    elif current_time - playhead_stuck_time >= 5.0:
                        # Playhead stuck for 5+ seconds
                        media_stuck = True
                else:
                    # Playhead moved, reset stuck timer
                    playhead_stuck_time = None
            
            # If media stuck and was previously playing, show off air message
            if media_stuck and media_was_playing:
                print("Media stopped (playhead stuck for 5+ seconds) - showing off air message")
                try:
                    c.setChannel(0)
                    c.deletePage(0x888)
                    c.openPage(0x888)
                    c.setSubPage(0x0000)
                    c.setSubPageOptions(erase=False, newsflash=False, subtitle=True, suppressHeader=False, 
                                        update=False, interruptedSequence=False, inhibit=False, 
                                        transmitPage=True, language=0, region=0, cycleTime=1, timedMode=False)
                    show_status_message(c, config['off_air_message'])
                    c.closePage()
                    off_air_shown = True
                except Exception as e:
                    print(f"Error showing off air message: {e}")
                media_was_playing = False
                current_media_file = None
                playhead_stuck_time = None
            
            # Check if media file has changed or restarted
            media_changed = False
            if media_file and media_file != current_media_file and play_head_ms is not None:
                print("Media file: " + media_file)
                media_changed = True
                current_media_file = media_file
                last_playhead = None
                playhead_stuck_time = None
                media_was_playing = True
                off_air_shown = False
            
            # Check if playhead reset (media restarted or changed)
            elif play_head_ms is not None and last_playhead is not None:
                if play_head_ms < 1000 and last_playhead > 5000:  # Reset detected
                    print("Media file: " + media_file)
                    media_changed = True
                    playhead_stuck_time = None
                    media_was_playing = True
                    off_air_shown = False
            
            # If media changed, process subtitles
            if media_changed and media_file:
                # Look for subtitle file
                base_path = os.path.splitext(media_file)[0]
                subtitle_file = base_path + '.srt'
                
                if os.path.exists(subtitle_file):
                    print("Subtitle file: " + subtitle_file)
                    
                    # Convert subtitle file to UTF-8 if needed
                    convert_to_utf8(subtitle_file)
                    
                    try:
                        # Transmit subtitles for this media
                        transmit_subtitles_obs(c, subtitle_file, obs_client, source_name, config.get("subtitle_delay", 0.0))
                    except Exception as sub_err:
                        import traceback
                        print(f"Error transmitting subtitles: {sub_err}")
                        traceback.print_exc()
                    
                    # When transmit_subtitles_obs returns, media has ended
                    print("Subtitle transmission ended - showing off air message")
                    try:
                        c.setChannel(0)
                        c.deletePage(0x888)
                        c.openPage(0x888)
                        c.setSubPage(0x0000)
                        c.setSubPageOptions(erase=False, newsflash=False, subtitle=True, suppressHeader=False, 
                                            update=False, interruptedSequence=False, inhibit=False, 
                                            transmitPage=True, language=0, region=0, cycleTime=1, timedMode=False)
                        show_status_message(c, config['off_air_message'])
                        c.closePage()
                        off_air_shown = True
                    except Exception as e:
                        print(f"Error showing off air message: {e}")
                    
                    media_was_playing = False
                    current_media_file = None
                else:
                    print("No subtitle file found, continuing without subtitles")
                    print("Broadcasting subtitles unavailable message")
                    
                    # Display subtitles unavailable message
                    try:
                        c.setChannel(0)
                        c.deletePage(0x888)
                        c.openPage(0x888)
                        c.setSubPage(0x0000)
                        c.setSubPageOptions(erase=False, newsflash=False, subtitle=True, suppressHeader=False, 
                                            update=False, interruptedSequence=False, inhibit=False, 
                                            transmitPage=True, language=0, region=0, cycleTime=1, timedMode=False)
                        show_status_message(c, config['subtitles_unavailable_message'])
                        c.closePage()
                    except:
                        pass
            
            # Update last playhead
            last_playhead = play_head_ms
            
            time.sleep(2)  # Check every 2 seconds
            
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"Error in media monitor: {e}")
            time.sleep(5)  # Wait longer on error

class OMX:
    def __init__(self, id="org.mpris.MediaPlayer2.omxplayer"):
        self.id = id
        
        done = 0
        retry = 0
        while done == 0:
            try:
                with open('/tmp/omxplayerdbus.pi', 'r+') as f:
                    omxplayerdbus = f.read().strip()
                    bus = dbus.bus.BusConnection(omxplayerdbus)
                    obj = bus.get_object(self.id, '/org/mpris/MediaPlayer2', introspect=False)
                    self.prop = dbus.Interface(obj, 'org.freedesktop.DBus.Properties')
                    self.volset = dbus.Interface(obj, 'org.freedesktop.DBus.Properties.Set')
                    self.key = dbus.Interface(obj, 'org.mpris.MediaPlayer2.Player')
                    done = 1
            except:
                retry += 1
                print("retry dbus " + self.id)
                time.sleep(1)
                if retry >= 30:
                    return "DBUS Retries exceeded"
                pass

    def play(self, ifile):
        try:
            p = Popen(["/usr/bin/omxplayer", "--no-osd", "--win", "2,10,718,576", "--alpha", "0", 
                      "--dbus_name", self.id, "--vol", "-6000", "--aspect-mode", "letterbox", 
                      "--aidx", "0", ifile], stdout=PIPE)
        except:
            print("Error in Popen")
            out, err = p.communicate()
            out = out.decode("utf-8").rstrip("\n")
            return out

        usr = getpass.getuser()
        done = 0
        retry = 0
        while done == 0:
            try:
                with open('/tmp/omxplayerdbus.' + usr, 'r+') as f:
                    omxplayerdbus = f.read().strip()
                    bus = dbus.bus.BusConnection(omxplayerdbus)
                    obj = bus.get_object(self.id, '/org/mpris/MediaPlayer2', introspect=False)
                    self.prop = dbus.Interface(obj, 'org.freedesktop.DBus.Properties')
                    self.volset = dbus.Interface(obj, 'org.freedesktop.DBus.Properties.Set')
                    self.key = dbus.Interface(obj, 'org.mpris.MediaPlayer2.Player')
                    done = 1
            except:
                retry += 1
                print("retry dbus " + self.id)
                time.sleep(1)
                if retry >= 30:
                    return "DBUS Retries exceeded"
                pass

    def pause(self):
        self.key.Action(dbus.Int32("16"))
        
    def run(self):
        self.key.Action(dbus.Int32("36"))

    def iquit(self):
        self.key.Action(dbus.Int32("15"))
        return

    def alpha(self, key):
        self.key.SetAlpha(dbus.ObjectPath("/not/used"), dbus.Int64(key))
        return
    
    def layer(self, key):
        self.key.SetLayer(dbus.Int64(key))
        return
    
    def setPosition(self, key):
        self.key.SetPosition(dbus.ObjectPath("/not/used"), dbus.Int64(key))
        return
    
    def position(self):
        return(int(self.prop.Position()))
    
    def volume(self, key):
        self.prop.Set(dbus.String("org.mpris.MediaPlayer2.Player"), 
                     dbus.String("Volume"), dbus.Double(key))
        return

if __name__ == "__main__":
    config = load_config()
    
    # Initialize teletext connection
    c = interface.Client(config["teletext_host"], config["teletext_port"])
    
    print(f"Running in {config['mode']} mode")
    
    try:
        if config["mode"] == "obs":
            if not OBS_AVAILABLE:
                print("Error: OBS mode selected but obswebsocket is not installed")
                exit(1)
            
            obs_client = obsws(config["obs_host"], config["obs_port"], config["obs_password"])
            obs_client.connect()
            
            print(obs_client.call(obs_requests.GetVersion()).getObsVersion())
            print(f"Monitoring OBS source: {config['obs_source_name']}")
            
            # Start monitoring for media changes
            monitor_obs_media(c, obs_client, config["obs_source_name"], config)
        
        elif config["mode"] == "omxplayer":
            if not DBUS_AVAILABLE:
                print("Error: omxplayer mode selected but dbus is not installed")
                exit(1)
                
            vt1 = OMX("org.mpris.MediaPlayer2.omxplayer")
            
            subtitle_file = "/mnt/media/Film & TV/Drama/A Touch of Frost/ITVHub/Series 1/A Touch of Frost - Care and Protection [Ya1774a0001].srt"
            
            if os.path.exists(subtitle_file):
                # Convert subtitle file to UTF-8 if needed
                convert_to_utf8(subtitle_file)
                
                transmit_subtitles_omx(c, subtitle_file, vt1, config.get("subtitle_delay", 0.0))
                
                while True:
                    time.sleep(1)
            else:
                print(f"Subtitle file not found: {subtitle_file}")
                print("Please update the subtitle_file path in the script or config")
        
        else:
            print(f"Unknown mode: {config['mode']}")
            print("Valid modes are: 'obs' or 'omxplayer'")
            exit(1)

    except KeyboardInterrupt:
        print("program stopping")
        
        try:
            c.setChannel(0)
            c.deletePage(0x888)
            c.openPage(0x888)
            c.setSubPage(0x0000)
            c.setSubPageOptions(erase=False, newsflash=False, subtitle=True, suppressHeader=False, 
                                update=False, interruptedSequence=False, inhibit=False, 
                                transmitPage=True, language=0, region=0, cycleTime=1, timedMode=False)
            show_status_message(c, config['subtitles_unavailable_message'])
            c.closePage()
        except:
            pass
        
        time.sleep(1)
    
    finally:
        try:
            c.setChannel(0)
            c.deletePage(0x888)
            c.openPage(0x888)
            c.setSubPage(0x0000)
            c.setSubPageOptions(erase=False, newsflash=False, subtitle=True, suppressHeader=False, 
                                update=False, interruptedSequence=False, inhibit=False, 
                                transmitPage=True, language=0, region=0, cycleTime=1, timedMode=False)
            show_status_message(c, config['off_air_message'])
            c.closePage()
        except:
            pass
        
        if config["mode"] == "obs" and 'obs_client' in locals():
            try:
                obs_client.disconnect()
            except Exception:
                pass