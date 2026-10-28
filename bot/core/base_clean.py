import os.path
import os
import re

def extract_base_anime_name(name: str) -> str:
    cleaned = name
    cleaned = re.sub(r'^\[[^\]]+\]\s*', '', cleaned)
    cleaned = re.sub(r'\s*-\s*\d+\s*(\([^\)]*\))?', '', cleaned)
    cleaned = re.sub(r'\s*S\d{1,2}E\d{1,3}', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s*Season\s*\d+', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s*Ep(isode)?\s*\d+', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\b(HEVC|x265|x264|AAC|WEB[- ]?DL|WEB[- ]?Rip|BluRay|HDTV|MultiSub|Multi-Subs|CR|RAW|SUB|Dub|Dual[- ]?Audio|Dual[- ]?Subs|VOSTFR|AMZN|NF|BILI|WeTV|iQ|AVC|EAC3|DDP2\.0|DDP5\.1|H\.?264|H\.?265|10bit|8bit|OV|REPACK|weekly|English Audio|VARYG|YURASUKA|Tsundere-Raws|Aniplus|YT|Disney\+|Opus|CA|Hero|Finals Arc|Uncensored|Remastered|A Multi-Language Release|Dual Audio|Dual Subs|Multi-Audio|Multi Subs|Dual|Eng|Jap|EN|JP)\b', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\b(\d{3,4}p)\b', '', cleaned)
    cleaned = re.sub(r'\.(mkv|mp4|avi|webm|wmv|mov|flv|ts)$', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\[[A-Fa-f0-9]{6,}\]', '', cleaned)
    cleaned = re.sub(r'\([^)]+\)', '', cleaned)
    cleaned = re.sub(r'\[[^\]]+\]', '', cleaned)
    cleaned = re.sub(r'[._\-:,/]+', ' ', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned)
    return cleaned.strip()