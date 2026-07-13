#!/usr/bin/env python3
"""
Word Dictation Buddy - SCF Web Function
Dependencies: Flask (bundled in vendor/), urllib (stdlib)
"""

import sys
import os

# Add vendored dependencies to path (for SCF deployment)
_vendor = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'vendor')
if not os.path.isdir(_vendor):
    _vendor = os.path.join(os.getcwd(), 'vendor')
if not os.path.isdir(_vendor):
    _vendor = os.path.join('/var/task', 'vendor')
if os.path.isdir(_vendor):
    sys.path.insert(0, _vendor)

import json
import re
import time
import uuid
import random
import hashlib
import hmac
import urllib.request
import urllib.parse
import urllib.error
try:
    import ssl
    _SSL_CTX = ssl.create_default_context()
except (ImportError, OSError):
    _SSL_CTX = None
from flask import Flask, request, jsonify, Response

# ==================== Configuration ====================
# ADMIN_PWD can be either plaintext or SHA256 hash (64 hex chars).
# If it's 64 hex chars, treat as hash directly; otherwise hash it.
ADMIN_PWD_CFG = os.environ.get('ADMIN_PWD', 'admin123')
_is_hash = len(ADMIN_PWD_CFG) == 64 and all(c in '0123456789abcdef' for c in ADMIN_PWD_CFG.lower())
ADMIN_PWD_VERIFY = ADMIN_PWD_CFG.lower() if _is_hash else hashlib.sha256(ADMIN_PWD_CFG.encode('utf-8')).hexdigest()
DEEPSEEK_KEY = os.environ.get('DEEPSEEK_API_KEY', '')
DEEPSEEK_URL = 'https://api.deepseek.com/v1/chat/completions'
COS_SID = os.environ.get('COS_SECRET_ID', '')
COS_SKEY = os.environ.get('COS_SECRET_KEY', '')
COS_BUCKET = os.environ.get('COS_BUCKET', 'kb-efm-analytics')
COS_REGION = os.environ.get('COS_REGION', 'ap-guangzhou')
COS_PREFIX = 'vocab-buddy/'
COS_HOST = f'{COS_BUCKET}.cos.{COS_REGION}.myqcloud.com'
COS_SCHEME = 'https' if _SSL_CTX else 'http'

app = Flask(__name__)

# CORS: allow frontend hosted on other domains (GitHub Pages, CloudStudio, etc.)
@app.after_request
def _cors(resp):
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, DELETE, OPTIONS'
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return resp

# ==================== COS Client ====================
class COSClient:
    """Minimal COS XML API client using urllib + HMAC-SHA1 signing"""

    def _sign(self, method, uri):
        now = int(time.time())
        key_time = f'{now - 60};{now + 600}'
        sign_key = hmac.new(
            COS_SKEY.encode('utf-8'), key_time.encode('utf-8'), hashlib.sha1
        ).hexdigest()
        fmt = f'{method.lower()}\n{uri}\n\nhost={COS_HOST}\n'
        digest = hashlib.sha1(fmt.encode('utf-8')).hexdigest()
        sts = f'sha1\n{key_time}\n{digest}\n'
        sig = hmac.new(
            sign_key.encode('utf-8'), sts.encode('utf-8'), hashlib.sha1
        ).hexdigest()
        return (
            f'q-sign-algorithm=sha1&q-ak={COS_SID}'
            f'&q-sign-time={key_time}&q-key-time={key_time}'
            f'&q-header-list=host&q-url-param-list=&q-signature={sig}'
        )

    def _req(self, method, key, data=None):
        uri = f'/{COS_PREFIX}{key}'
        url = f'{COS_SCHEME}://{COS_HOST}{uri}'
        headers = {'Host': COS_HOST, 'Authorization': self._sign(method, uri)}
        if data is not None:
            if isinstance(data, (dict, list)):
                data = json.dumps(data, ensure_ascii=False).encode('utf-8')
            elif isinstance(data, str):
                data = data.encode('utf-8')
            headers['Content-Type'] = 'application/json'
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            kwargs = {'timeout': 30}
            if _SSL_CTX:
                kwargs['context'] = _SSL_CTX
            with urllib.request.urlopen(req, **kwargs) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except Exception as e:
            return 0, str(e).encode('utf-8')

    def get_json(self, key, default=None):
        status, body = self._req('GET', key)
        if status == 200:
            try:
                return json.loads(body.decode('utf-8'))
            except Exception:
                return default
        return default

    def put_json(self, key, data):
        status, _ = self._req('PUT', key, data)
        return status == 200

    def delete(self, key):
        status, _ = self._req('DELETE', key)
        return status in (200, 204)

cos = COSClient()

# ==================== Utilities ====================
def hash_pwd(pwd):
    return hashlib.sha256(pwd.encode('utf-8')).hexdigest()

def gen_id():
    return uuid.uuid4().hex[:8]

def gen_code():
    return str(random.randint(1000, 9999))

def ok(data=None, msg='ok'):
    return jsonify({'code': 0, 'msg': msg, 'data': data})

def fail(msg='error', code=1):
    return jsonify({'code': code, 'msg': msg, 'data': None})

def extract_json(text):
    """Extract JSON from LLM response (handles markdown code blocks)"""
    if not text:
        return None
    text = text.strip()
    if text.startswith('```'):
        lines = text.split('\n')
        lines = [l for l in lines if not l.strip().startswith('```')]
        text = '\n'.join(lines)
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r'[\[{].*[\]}]', text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except Exception:
                pass
    return None

# ==================== DeepSeek API ====================
def deepseek(prompt, system=None, timeout=30, max_tokens=4096, call_type='general'):
    if not DEEPSEEK_KEY:
        log_usage('ai_skip', call_type, reason='no_key')
        return None
    messages = []
    if system:
        messages.append({'role': 'system', 'content': system})
    messages.append({'role': 'user', 'content': prompt})
    body = json.dumps({
        'model': 'deepseek-chat',
        'messages': messages,
        'temperature': 0.3,
        'max_tokens': max_tokens
    }).encode('utf-8')
    req = urllib.request.Request(
        DEEPSEEK_URL, data=body,
        headers={'Authorization': f'Bearer {DEEPSEEK_KEY}', 'Content-Type': 'application/json'},
        method='POST'
    )
    try:
        _kw = {'timeout': timeout}
        if _SSL_CTX:
            _kw['context'] = _SSL_CTX
        with urllib.request.urlopen(req, **_kw) as resp:
            result = json.loads(resp.read().decode('utf-8'))
            usage = result.get('usage', {})
            log_usage('ai_call', call_type,
                      prompt_tokens=usage.get('prompt_tokens', 0),
                      completion_tokens=usage.get('completion_tokens', 0))
            return result['choices'][0]['message']['content']
    except Exception as e:
        print(f'DeepSeek error: {e}')
        log_usage('ai_error', call_type, error=str(e)[:200])
        return None

# ==================== Usage Tracking ====================
_usage_cache = None  # in-memory cache, synced to COS

def log_usage(event, sub_type, **extra):
    """Log usage events to COS. Non-blocking best-effort — never raises."""
    global _usage_cache
    try:
        if _usage_cache is None:
            _usage_cache = cos.get_json('config/usage_stats.json') or {
                'ai_calls': {'check': 0, 'generate': 0, 'training': 0, 'init_vocab': 0, 'general': 0},
                'ai_errors': 0,
                'ai_skips': 0,
                'total_prompt_tokens': 0,
                'total_completion_tokens': 0,
                'recent': [],
                'first_call': int(time.time()),
                'last_call': None
            }
        stats = _usage_cache
        entry = {
            'ts': int(time.time()),
            'event': event,
            'type': sub_type,
            **extra
        }
        if event == 'ai_call':
            stats['ai_calls'][sub_type] = stats['ai_calls'].get(sub_type, 0) + 1
            stats['total_prompt_tokens'] += extra.get('prompt_tokens', 0)
            stats['total_completion_tokens'] += extra.get('completion_tokens', 0)
        elif event == 'ai_error':
            stats['ai_errors'] += 1
        elif event == 'ai_skip':
            stats['ai_skips'] += 1
        stats['last_call'] = entry['ts']
        stats['recent'].append(entry)
        stats['recent'] = stats['recent'][-100:]
        cos.put_json('config/usage_stats.json', stats)
    except Exception:
        pass  # usage logging must never break the app

def get_usage_stats():
    global _usage_cache
    if _usage_cache is None:
        _usage_cache = cos.get_json('config/usage_stats.json')
    if _usage_cache is None:
        return {
            'ai_calls': {}, 'ai_errors': 0, 'ai_skips': 0,
            'total_prompt_tokens': 0, 'total_completion_tokens': 0,
            'recent': [], 'first_call': None, 'last_call': None
        }
    return _usage_cache

# ==================== Data Helpers ====================
def get_config():
    cfg = cos.get_json('config/system.json')
    if cfg is None:
        cfg = {'default_replay_limit': 5, 'tts_provider': 'browser', 'created_at': int(time.time())}
        cos.put_json('config/system.json', cfg)
    return cfg

def get_families():
    return cos.get_json('families_index.json', [])

def save_families(data):
    return cos.put_json('families_index.json', data)

def get_users(family_id):
    return cos.get_json(f'families/{family_id}/users.json', None)

def save_users(family_id, data):
    return cos.put_json(f'families/{family_id}/users.json', data)

def get_units(family_id):
    return cos.get_json(f'families/{family_id}/units.json', [])

def save_units(family_id, data):
    cos.put_json(f'families/{family_id}/units.json', data)

def get_records(family_id):
    return cos.get_json(f'families/{family_id}/records.json', [])

def save_records(family_id, data):
    cos.put_json(f'families/{family_id}/records.json', data)

def get_session(code):
    return cos.get_json(f'sessions/{code}.json', None)

def save_session(code, data):
    cos.put_json(f'sessions/{code}.json', data)

def delete_session(code):
    cos.delete(f'sessions/{code}.json')

def check_parent_pwd_global(pwd_hash, exclude_family=None):
    """Check if parent password is unique across all families"""
    families = get_families()
    for f in families:
        if exclude_family and f['family_id'] == exclude_family:
            continue
        users = get_users(f['family_id'])
        if users and users.get('parent', {}).get('password_hash') == pwd_hash:
            return False
    return True

# ==================== Grade Vocabulary ====================
BASIC_VOCAB = {
    'grade_6': [
        'about', 'above', 'across', 'active', 'actor', 'address', 'afraid', 'afternoon',
        'against', 'airport', 'almost', 'alone', 'along', 'already', 'animal', 'another',
        'answer', 'apple', 'around', 'arrive', 'basketball', 'beautiful', 'because',
        'before', 'begin', 'behind', 'believe', 'between', 'birthday', 'borrow', 'breakfast',
        'bridge', 'bring', 'brother', 'building', 'businessman', 'camera', 'careful',
        'carry', 'celebrate', 'children', 'choose', 'classmate', 'classroom', 'climb',
        'clothes', 'collect', 'computer', 'conversation', 'cook', 'correct', 'country',
        'cousin', 'dangerous', 'daughter', 'delicious', 'dictionary', 'different', 'difficult',
        'dumpling', 'electricity', 'elephant', 'engineer', 'enjoy', 'everyone', 'example',
        'excited', 'exercise', 'expensive', 'favourite', 'festival', 'foreign', 'forest',
        'friendly', 'garden', 'grandfather', 'grandmother', 'happy', 'healthy', 'holiday',
        'homework', 'hospital', 'important', 'interesting', 'internet', 'interview',
        'journalist', 'kind', 'kitchen', 'language', 'library', 'machine', 'magazine',
        'medicine', 'museum', 'musician', 'neighbor', 'noodle', 'octopus', 'painting',
        'parent', 'partner', 'patient', 'penguin', 'picnic', 'pilot', 'policeman',
        'polite', 'popular', 'practice', 'present', 'pretty', 'programme', 'question',
        'restaurant', 'sandwich', 'scientist', 'secretary', 'should', 'sometimes',
        'spring', 'station', 'straight', 'strawberry', 'student', 'sugar', 'summer',
        'sunshine', 'supermarket', 'surprise', 'swimming', 'teacher', 'telephone',
        'temperature', 'ticket', 'tiger', 'tomato', 'tomorrow', 'toothache', 'tourist',
        'traditional', 'travel', 'trousers', 'understand', 'uniform', 'vacation',
        'vegetable', 'village', 'visit', 'waiter', 'weather', 'weekend', 'winter',
        'wonderful', 'yesterday'
    ],
    'grade_7': [
        'ability', 'abroad', 'accept', 'accident', 'achieve', 'address', 'advantage',
        'adventure', 'advice', 'advise', 'afford', 'against', 'agreement', 'aim',
        'allow', 'almost', 'alone', 'already', 'amazing', 'among', 'ancient', 'anger',
        'angry', 'announce', 'anxious', 'anyone', 'appear', 'area', 'argue', 'army',
        'article', 'astronaut', 'athlete', 'attack', 'attention', 'attitude', 'attract',
        'audience', 'available', 'average', 'awake', 'award', 'aware', 'balance',
        'baseline', 'battle', 'beauty', 'belief', 'belong', 'benefit', 'beyond',
        'biology', 'blank', 'blood', 'border', 'brain', 'branch', 'breath', 'bright',
        'broadcast', 'calm', 'campaign', 'cancer', 'capture', 'career', 'careful',
        'celebrate', 'ceremony', 'challenge', 'champion', 'chance', 'change', 'character',
        'cheer', 'choice', 'choose', 'circle', 'classic', 'clean', 'climate', 'climb',
        'close', 'cloud', 'coach', 'coast', 'collect', 'college', 'comfort', 'command',
        'common', 'community', 'compare', 'compete', 'complete', 'concern', 'condition',
        'conference', 'confirm', 'connect', 'consider', 'continue', 'control', 'convenient',
        'conversation', 'correct', 'cost', 'count', 'courage', 'course', 'create',
        'creature', 'culture', 'curious', 'daily', 'damage', 'danger', 'decide',
        'decision', 'defend', 'degree', 'delicious', 'describe', 'design', 'desire',
        'detail', 'develop', 'dialogue', 'diet', 'difference', 'difficult', 'direct',
        'direction', 'discover', 'discuss', 'disease', 'divide', 'double', 'doubt',
        'dream', 'drop', 'education', 'effort', 'electric', 'emergency', 'emotion',
        'employ', 'encourage', 'energy', 'engine', 'enjoy', 'enough', 'enter',
        'entire', 'environment', 'especially', 'event', 'examine', 'excellent',
        'except', 'exciting', 'experience', 'experiment', 'explain', 'explore',
        'express', 'extra', 'extreme', 'fact', 'factory', 'failure', 'famous',
        'fault', 'favourite', 'fear', 'feature', 'festival', 'field', 'fight',
        'figure', 'final', 'finger', 'fire', 'firm', 'flight', 'focus', 'follow',
        'fool', 'foreign', 'forest', 'forever', 'forget', 'forgive', 'form', 'former',
        'fortune', 'forward', 'freedom', 'fresh', 'friendship', 'future', 'general',
        'generation', 'gentle', 'global', 'golden', 'government', 'grade', 'gradually',
        'grass', 'great', 'ground', 'group', 'grow', 'guard', 'guess', 'guest',
        'guide', 'guilty', 'habit', 'hand', 'handle', 'handsome', 'happen', 'hardly',
        'hate', 'headache', 'health', 'healthy', 'hear', 'heavy', 'height', 'helpful',
        'hero', 'hide', 'honest', 'honour', 'hope', 'huge', 'human', 'humour',
        'hurry', 'imagine', 'immediately', 'importance', 'improve', 'include',
        'increase', 'independent', 'influence', 'information', 'injure', 'inner',
        'inside', 'insist', 'instead', 'interest', 'interview', 'introduce', 'invent',
        'invite', 'island', 'journey', 'judge', 'junior', 'justice', 'keeper',
        'knowledge', 'laboratory', 'landscape', 'language', 'later', 'laugh', 'law',
        'leader', 'learn', 'leave', 'lecture', 'level', 'library', 'licence', 'light',
        'limit', 'list', 'local', 'lonely', 'loose', 'lucky', 'machine', 'magic',
        'main', 'major', 'manage', 'mark', 'market', 'master', 'match', 'material',
        'matter', 'maybe', 'meal', 'mean', 'meaning', 'meanwhile', 'medical',
        'medicine', 'memory', 'mention', 'message', 'method', 'middle', 'might',
        'million', 'mind', 'minute', 'mirror', 'mission', 'model', 'modern',
        'moment', 'money', 'monitor', 'month', 'mood', 'mountain', 'movement',
        'movie', 'museum', 'mystery', 'natural', 'nature', 'necessary', 'neighbour',
        'nervous', 'network', 'news', 'newspaper', 'normal', 'notice', 'novel',
        'nowadays', 'number', 'object', 'observe', 'obvious', 'occasion', 'offer',
        'officer', 'online', 'operation', 'opinion', 'opportunity', 'ordinary',
        'organize', 'original', 'outdoor', 'outer', 'outside', 'overcome', 'owner',
        'pain', 'painting', 'particular', 'partner', 'passage', 'passenger', 'patient',
        'pattern', 'peace', 'performance', 'perhaps', 'period', 'permit', 'personal',
        'persuade', 'phone', 'photo', 'physical', 'pick', 'pilot', 'pioneer',
        'planet', 'plant', 'platform', 'pleasure', 'poem', 'poet', 'police',
        'policy', 'popular', 'population', 'position', 'possible', 'potato', 'pound',
        'practice', 'praise', 'prefer', 'prepare', 'present', 'press', 'pretend',
        'pretty', 'prevent', 'price', 'pride', 'primary', 'private', 'prize',
        'probably', 'problem', 'process', 'produce', 'professor', 'programme',
        'project', 'promise', 'protect', 'proud', 'prove', 'provide', 'public',
        'publish', 'purpose', 'puzzle', 'quality', 'quantity', 'quarter', 'quickly',
        'quiet', 'quite', 'race', 'radio', 'railway', 'rainy', 'raise', 'range',
        'rather', 'reach', 'react', 'ready', 'realize', 'reason', 'receive',
        'recent', 'record', 'recycle', 'reduce', 'refuse', 'regard', 'regret',
        'relation', 'relax', 'remain', 'remember', 'remove', 'repair', 'repeat',
        'reply', 'report', 'research', 'resource', 'respect', 'responsible', 'rest',
        'result', 'return', 'review', 'reward', 'rich', 'ride', 'right', 'rise',
        'risk', 'robot', 'rock', 'role', 'rough', 'rule', 'safety', 'salary',
        'salesman', 'satisfy', 'save', 'scene', 'science', 'scientist', 'screen',
        'search', 'season', 'seat', 'secret', 'section', 'secure', 'sense',
        'series', 'serious', 'servant', 'serve', 'service', 'settle', 'several',
        'shadow', 'shake', 'shape', 'share', 'sharp', 'sheep', 'shelter', 'shine',
        'ship', 'shock', 'short', 'shoulder', 'shout', 'show', 'shut', 'sick',
        'side', 'sight', 'sign', 'signal', 'silent', 'silly', 'silver', 'simple',
        'since', 'sing', 'single', 'sir', 'situation', 'size', 'sleep', 'smart',
        'smell', 'smile', 'smooth', 'social', 'society', 'soft', 'soil', 'soldier',
        'solution', 'solve', 'somewhere', 'sort', 'sound', 'source', 'space',
        'speak', 'special', 'speech', 'speed', 'spell', 'spend', 'spirit', 'spoon',
        'sport', 'spread', 'spring', 'square', 'stage', 'stamp', 'stand', 'standard',
        'star', 'start', 'state', 'station', 'stay', 'steam', 'steel', 'step',
        'stick', 'still', 'stomach', 'stone', 'stop', 'store', 'storm', 'story',
        'strange', 'street', 'strong', 'struggle', 'student', 'study', 'stupid',
        'subject', 'succeed', 'success', 'sudden', 'suffer', 'suggest', 'suitable',
        'summer', 'support', 'suppose', 'sure', 'surface', 'surprise', 'survive',
        'sweet', 'swim', 'system', 'table', 'taste', 'teach', 'team', 'technology',
        'telephone', 'television', 'tell', 'temperature', 'temple', 'term', 'test',
        'text', 'than', 'thank', 'theatre', 'their', 'them', 'themselves', 'then',
        'theory', 'there', 'thick', 'thin', 'think', 'though', 'thought', 'thousand',
        'threat', 'through', 'throw', 'Thursday', 'ticket', 'tiger', 'time', 'tiny',
        'tired', 'today', 'together', 'tonight', 'tooth', 'topic', 'total', 'touch',
        'tour', 'toward', 'tower', 'town', 'trade', 'traditional', 'traffic',
        'train', 'translate', 'transport', 'travel', 'treasure', 'treat', 'tree',
        'trick', 'trip', 'troops', 'trouble', 'trust', 'truth', 'try', 'turn',
        'twice', 'type', 'typical', 'ugly', 'umbrella', 'uncle', 'under', 'underground',
        'understand', 'unfair', 'uniform', 'union', 'unit', 'unite', 'universe',
        'university', 'unknown', 'unless', 'until', 'unusual', 'upset', 'useful',
        'usual', 'valley', 'valuable', 'value', 'vary', 'vehicle', 'victory',
        'video', 'view', 'village', 'violence', 'violin', 'virtual', 'visible',
        'vision', 'visit', 'visitor', 'voice', 'volunteer', 'wait', 'wake', 'walk',
        'wall', 'wander', 'want', 'war', 'warm', 'warn', 'waste', 'watch',
        'water', 'wave', 'way', 'weak', 'wealth', 'weapon', 'wear', 'weather',
        'web', 'week', 'weight', 'welcome', 'well', 'west', 'western', 'wet',
        'what', 'wheat', 'wheel', 'when', 'wherever', 'whether', 'while', 'whisper',
        'white', 'whole', 'whose', 'wide', 'widely', 'wife', 'wild', 'will',
        'win', 'wind', 'window', 'wine', 'wing', 'winner', 'winter', 'wise',
        'wish', 'within', 'without', 'wolf', 'woman', 'wonder', 'wood', 'wool',
        'word', 'work', 'worker', 'world', 'worry', 'worse', 'worth', 'would',
        'wound', 'wrap', 'wrong', 'yard', 'year', 'yellow', 'yesterday', 'yet',
        'young', 'youth', 'zero', 'zone'
    ],
    'grade_8': [
        'absolute', 'abstract', 'academic', 'accomplish', 'account', 'accumulate',
        'accurate', 'accuse', 'accustom', 'achieve', 'acknowledge', 'adapt', 'adequate',
        'adjust', 'administration', 'admire', 'admit', 'adopt', 'advanced', 'advocate',
        'aesthetic', 'affect', 'affection', 'agency', 'aggressive', 'agriculture',
        'alarm', 'alcohol', 'alike', 'allergic', 'allocate', 'allowance', 'alphabet',
        'alternative', 'amazing', 'ambassador', 'ambiguous', 'ambition', 'amend',
        'analyze', 'ancestor', 'ancient', 'announcement', 'annual', 'anonymous',
        'anticipate', 'anxiety', 'apologize', 'apparent', 'appeal', 'appetite',
        'appliance', 'apply', 'appoint', 'appreciate', 'approach', 'appropriate',
        'approve', 'approximate', 'architect', 'archive', 'arithmetic', 'arrange',
        'artificial', 'artistic', 'aspect', 'assemble', 'assess', 'assign',
        'assist', 'associate', 'assume', 'assure', 'astronomy', 'atmosphere',
        'attempt', 'attend', 'attitude', 'attraction', 'auction', 'authentic',
        'authority', 'autobiography', 'automatic', 'autonomous', 'available',
        'avenue', 'average', 'avoid', 'await', 'award', 'aware', 'background',
        'bacteria', 'bakery', 'balance', 'bandage', 'bankrupt', 'barrier', 'basis',
        'battery', 'bear', 'behalf', 'behave', 'behaviour', 'believe', 'belong',
        'beneath', 'benefit', 'besides', 'betray', 'biology', 'birthplace',
        'blame', 'blank', 'blast', 'bleed', 'bless', 'blind', 'block', 'bloom',
        'board', 'boil', 'bonus', 'bookmark', 'boom', 'boost', 'border', 'bore',
        'botany', 'bounce', 'bound', 'boundary', 'brake', 'brand', 'brave',
        'breakdown', 'breakthrough', 'breed', 'brief', 'brilliant', 'broad',
        'broadcast', 'browse', 'budget', 'burden', 'bureau', 'burst', 'bury',
        'cabin', 'cable', 'campaign', 'cancel', 'candidate', 'capable', 'capacity',
        'capital', 'capture', 'career', 'careful', 'casual', 'catalogue', 'category',
        'catholic', 'cause', 'caution', 'celebrate', 'cement', 'central', 'ceremony',
        'certificate', 'challenge', 'champion', 'channel', 'chaos', 'chapter',
        'characteristic', 'charge', 'chart', 'chase', 'cheat', 'chef', 'chemical',
        'chew', 'childhood', 'choice', 'circuit', 'circumstance', 'citizen',
        'civil', 'civilian', 'claim', 'clarify', 'classic', 'classify', 'clear',
        'client', 'climate', 'clinic', 'clock', 'closet', 'clue', 'cluster',
        'coach', 'coast', 'code', 'cognitive', 'collaborate', 'collapse', 'colleague',
        'combine', 'comedy', 'comfort', 'command', 'comment', 'commerce',
        'commission', 'commit', 'committee', 'communicate', 'community', 'companion',
        'compare', 'compete', 'competent', 'complaint', 'complete', 'complex',
        'component', 'compose', 'composition', 'comprehension', 'conceal', 'concentrate',
        'concept', 'concern', 'conclude', 'concrete', 'condemn', 'condition',
        'conduct', 'conference', 'confidence', 'confirm', 'conflict', 'confront',
        'confuse', 'congress', 'connect', 'conscience', 'conscious', 'consensus',
        'consequence', 'conservation', 'consider', 'consist', 'constant', 'constitute',
        'construct', 'consult', 'consume', 'contact', 'contain', 'contemporary',
        'content', 'contest', 'context', 'continent', 'continue', 'contract',
        'contradict', 'contrast', 'contribute', 'controversial', 'convenient',
        'convention', 'convert', 'convince', 'cooperate', 'coordinate', 'copyright',
        'corporate', 'correct', 'correspond', 'council', 'counsel', 'counter',
        'courage', 'court', 'crash', 'create', 'creative', 'creature', 'credit',
        'crew', 'crime', 'crisis', 'criterion', 'critical', 'criticism', 'crucial',
        'cultivate', 'cultural', 'cure', 'curiosity', 'current', 'curriculum',
        'curtain', 'custom', 'cyber', 'cycle', 'daily', 'damage', 'damp',
        'debate', 'decade', 'decay', 'deceive', 'decent', 'decide', 'declare',
        'decline', 'decorate', 'decrease', 'dedicate', 'define', 'delay',
        'delegate', 'deliberate', 'delicate', 'deliver', 'demand', 'democratic',
        'demonstrate', 'deny', 'depart', 'dependent', 'deposit', 'depress',
        'derive', 'descend', 'describe', 'deserve', 'design', 'desire', 'despair',
        'desperate', 'despite', 'destination', 'destroy', 'detail', 'detect',
        'determine', 'develop', 'device', 'devote', 'dialect', 'diameter',
        'diploma', 'direct', 'disadvantage', 'disagree', 'disappear', 'disappoint',
        'disaster', 'discard', 'discipline', 'disclose', 'discount', 'discover',
        'discuss', 'disease', 'dismiss', 'display', 'dispute', 'distinct',
        'distinguish', 'distribute', 'disturb', 'diverse', 'divide', 'doctrine',
        'domestic', 'dominate', 'donate', 'dose', 'doubt', 'drama', 'dramatic',
        'drown', 'duration', 'dwell', 'dynamic', 'eager', 'earn', 'earthquake',
        'ease', 'ecology', 'economic', 'economy', 'edit', 'edition', 'effect',
        'efficient', 'effort', 'elaborate', 'elect', 'electronic', 'elegant',
        'element', 'embarrass', 'embrace', 'emerge', 'emergency', 'emotion',
        'emphasis', 'employ', 'encounter', 'encourage', 'endure', 'energetic',
        'enforce', 'engage', 'enhance', 'enormous', 'ensure', 'enterprise',
        'entertain', 'enthusiasm', 'entitle', 'entry', 'environment', 'episode',
        'equal', 'equip', 'era', 'error', 'escape', 'essential', 'establish',
        'estate', 'estimate', 'ethical', 'evaluate', 'eventually', 'evidence',
        'evolve', 'exaggerate', 'examine', 'exceed', 'excellent', 'exception',
        'excessive', 'exchange', 'excite', 'exclude', 'excuse', 'execute',
        'exhaust', 'exhibit', 'exist', 'expand', 'expect', 'expense', 'experience',
        'experiment', 'expert', 'exploit', 'explore', 'export', 'expose', 'extend',
        'extent', 'external', 'extraordinary', 'extreme', 'facility', 'factor',
        'faculty', 'failure', 'faint', 'faith', 'fame', 'familiar', 'famine',
        'fancy', 'fantasy', 'fascinate', 'fashion', 'fatal', 'fate', 'fault',
        'favour', 'feasible', 'feature', 'federal', 'fee', 'fiction', 'fierce',
        'figure', 'filter', 'final', 'finance', 'finger', 'fireplace', 'firm',
        'fiscal', 'fix', 'flame', 'flash', 'flat', 'flavour', 'flee', 'flexible',
        'float', 'flood', 'flourish', 'fluent', 'focus', 'forecast', 'foreign',
        'forge', 'formal', 'format', 'former', 'fortune', 'foundation', 'fraction',
        'fragment', 'frame', 'frank', 'fraud', 'free', 'frequency', 'friction',
        'frighten', 'frontier', 'frustrate', 'function', 'fundamental', 'furnish',
        'furthermore', 'gain', 'gallery', 'gap', 'garage', 'garbage', 'gas',
        'gather', 'gender', 'gene', 'general', 'generate', 'generous', 'genetic',
        'genius', 'genuine', 'gesture', 'giant', 'gift', 'glance', 'glimpse',
        'global', 'glory', 'govern', 'grace', 'gradual', 'graduate', 'grain',
        'grant', 'grasp', 'grateful', 'gravity', 'greet', 'grief', 'grind',
        'guarantee', 'guilt', 'habitat', 'halt', 'hammer', 'handle', 'handsome',
        'handy', 'happen', 'harbour', 'hardship', 'hardware', 'harm', 'harmony',
        'harsh', 'haste', 'hate', 'haul', 'heal', 'headline', 'headquarters',
        'health', 'heap', 'hesitate', 'highlight', 'hike', 'hint', 'historian',
        'hollow', 'holy', 'honour', 'horizon', 'horrible', 'horror', 'host',
        'hostile', 'household', 'humble', 'humour', 'hunt', 'hurry', 'identify',
        'identity', 'ignorant', 'ignore', 'illegal', 'illusion', 'illustrate',
        'image', 'imitate', 'immense', 'impact', 'implement', 'implication',
        'imply', 'import', 'impose', 'impress', 'incident', 'incline', 'include',
        'income', 'increase', 'incredible', 'independence', 'index', 'indicate',
        'individual', 'induce', 'industrial', 'inevitable', 'infect', 'inferior',
        'inflation', 'influence', 'inform', 'infrastructure', 'ingredient',
        'initial', 'initiative', 'innocent', 'innovate', 'input', 'inquire',
        'insane', 'insect', 'insert', 'insight', 'inspect', 'inspire', 'install',
        'instance', 'instant', 'instinct', 'institute', 'instruct', 'instrument',
        'insult', 'insurance', 'intellectual', 'intelligence', 'intend', 'intense',
        'interact', 'interfere', 'interior', 'internal', 'interpret', 'interrupt',
        'interval', 'interview', 'intimate', 'invade', 'invest', 'investigate',
        'invite', 'involve', 'irony', 'isolate', 'issue', 'item', 'jail',
        'jealous', 'journal', 'journey', 'joy', 'judgement', 'junior', 'justice',
        'justify', 'keen', 'kindness', 'kingdom', 'kneel', 'knit', 'label',
        'laboratory', 'lack', 'ladder', 'lag', 'landscape', 'launch', 'laundry',
        'lawsuit', 'layer', 'leadership', 'league', 'lean', 'leap', 'lecture',
        'legal', 'legend', 'leisure', 'lens', 'lesson', 'liable', 'liberal',
        'liberty', 'licence', 'lifestyle', 'lifetime', 'limitation', 'link',
        'liquid', 'literacy', 'literary', 'literature', 'load', 'loan', 'locate',
        'lodge', 'logic', 'loyal', 'luxury', 'magnet', 'magnify', 'maintain',
        'major', 'majority', 'manipulate', 'manner', 'manual', 'manufacture',
        'manuscript', 'margin', 'marine', 'market', 'mass', 'mature', 'maximum',
        'meadow', 'means', 'measure', 'mechanic', 'media', 'medium', 'memorial',
        'mental', 'merchant', 'mercy', 'mere', 'merely', 'merry', 'method',
        'microscope', 'mild', 'military', 'mill', 'mineral', 'minimum', 'minor',
        'minute', 'miracle', 'mirror', 'miserable', 'mislead', 'mission',
        'misunderstand', 'mix', 'mobile', 'moderate', 'modify', 'moisture',
        'molecule', 'monitor', 'monument', 'mood', 'moral', 'mortgage', 'motion',
        'motivate', 'mould', 'mount', 'mourn', 'movement', 'multiple', 'muscle',
        'museum', 'mutual', 'mystery', 'myth', 'naked', 'narrow', 'nasty',
        'nation', 'native', 'natural', 'nature', 'navigate', 'negative', 'neglect',
        'negotiate', 'nervous', 'network', 'neutral', 'nonsense', 'normal',
        'notable', 'notice', 'notion', 'novel', 'nuclear', 'nuisance', 'number',
        'numerous', 'nursery', 'nutrition', 'obey', 'object', 'obligation',
        'obstacle', 'obtain', 'obvious', 'occasion', 'occupation', 'occupy',
        'occur', 'odd', 'odds', 'offend', 'official', 'offset', 'ongoing',
        'online', 'operate', 'opinion', 'opportunity', 'oppose', 'opposite',
        'option', 'oral', 'orbit', 'order', 'ordinary', 'organ', 'organic',
        'organize', 'origin', 'outcome', 'outdoor', 'outline', 'output',
        'outstanding', 'overall', 'overcome', 'overlook', 'overseas', 'overweight',
        'owner', 'oxygen', 'package', 'pain', 'palace', 'pale', 'panel',
        'panic', 'paragraph', 'parallel', 'parcel', 'pardon', 'parliament',
        'partial', 'participate', 'particular', 'partner', 'passage', 'passion',
        'passive', 'passport', 'patience', 'pattern', 'pause', 'pave', 'payment',
        'peace', 'peak', 'penalty', 'pend', 'pension', 'perceive', 'percentage',
        'perform', 'permanent', 'permit', 'persist', 'personality', 'perspective',
        'persuade', 'phase', 'phenomenon', 'philosophy', 'photograph', 'physical',
        'physician', 'pile', 'pilot', 'pioneer', 'pipe', 'pitch', 'planet',
        'plant', 'plate', 'platform', 'pleasure', 'pledge', 'plot', 'plug',
        'plunge', 'poetry', 'poison', 'policy', 'polish', 'politics', 'pollute',
        'pool', 'popular', 'portable', 'portion', 'portrait', 'position',
        'positive', 'possess', 'possibility', 'postpone', 'potential', 'poverty',
        'practical', 'precaution', 'precede', 'predict', 'prefer', 'pregnant',
        'prejudice', 'premier', 'preparation', 'prescribe', 'presence', 'preserve',
        'pressure', 'pretend', 'prevail', 'prevent', 'previous', 'primary',
        'prime', 'primitive', 'principal', 'principle', 'prior', 'priority',
        'private', 'procedure', 'process', 'proclaim', 'produce', 'profession',
        'professional', 'profile', 'profit', 'progress', 'prohibit', 'project',
        'promote', 'prompt', 'proof', 'proper', 'property', 'proportion',
        'propose', 'prospect', 'prosperity', 'protect', 'protest', 'provide',
        'province', 'publish', 'pulse', 'punch', 'punish', 'purchase', 'pure',
        'pursue', 'puzzle', 'qualify', 'quality', 'quantity', 'quarter',
        'quit', 'quotation', 'race', 'racial', 'radiation', 'radical', 'rage',
        'raid', 'rail', 'rainbow', 'raise', 'range', 'rank', 'rapid', 'rare',
        'rate', 'rather', 'raw', 'react', 'readily', 'realistic', 'reality',
        'realize', 'reason', 'rebel', 'recall', 'receipt', 'receive', 'recent',
        'reception', 'recipe', 'recognition', 'recognize', 'recommend', 'record',
        'recover', 'recreation', 'recruit', 'reduce', 'refer', 'reflect',
        'reform', 'refresh', 'refuge', 'refuse', 'regard', 'region', 'register',
        'regret', 'regular', 'regulate', 'reinforce', 'reject', 'relate',
        'relation', 'relative', 'relax', 'release', 'relevant', 'reliable',
        'relief', 'relieve', 'rely', 'remain', 'remark', 'remarkable', 'remedy',
        'remind', 'remote', 'remove', 'render', 'renew', 'rent', 'repair',
        'repeat', 'replace', 'reply', 'report', 'represent', 'reproduce',
        'reputation', 'request', 'require', 'rescue', 'resemble', 'reserve',
        'residence', 'resign', 'resist', 'resolve', 'resource', 'respect',
        'respond', 'response', 'responsibility', 'restore', 'restrict', 'result',
        'retain', 'retire', 'retreat', 'return', 'reveal', 'revenue', 'reverse',
        'review', 'revise', 'revolt', 'revolution', 'reward', 'rhythm', 'ridiculous',
        'rigid', 'ripe', 'risk', 'ritual', 'rival', 'roar', 'robust', 'rocket',
        'romantic', 'rough', 'route', 'routine', 'ruin', 'sacred', 'sacrifice',
        'sake', 'salary', 'sample', 'satellite', 'satisfy', 'scale', 'scan',
        'scandal', 'scarce', 'scatter', 'scene', 'schedule', 'scheme', 'scholar',
        'scientific', 'scope', 'scratch', 'screen', 'script', 'seal', 'search',
        'section', 'sector', 'secure', 'seek', 'seize', 'select', 'self',
        'semester', 'seminar', 'senior', 'sense', 'sensitive', 'sentence',
        'separate', 'sequence', 'series', 'serious', 'settle', 'settlement',
        'severe', 'shadow', 'shallow', 'shame', 'shape', 'shed', 'shelter',
        'shift', 'shine', 'shock', 'shore', 'shortage', 'shoulder', 'shout',
        'show', 'shrink', 'signal', 'significant', 'silence', 'silly', 'similar',
        'simplify', 'sincere', 'site', 'situation', 'sketch', 'skill', 'slap',
        'slavery', 'slight', 'slow', 'smart', 'smooth', 'so-called', 'social',
        'society', 'software', 'solar', 'sole', 'solid', 'solution', 'solve',
        'somehow', 'somewhat', 'sophisticated', 'sort', 'source', 'space',
        'special', 'species', 'specific', 'spectacular', 'speech', 'speed',
        'spend', 'spill', 'spin', 'spirit', 'spiritual', 'split', 'sponsor',
        'spontaneous', 'spread', 'stable', 'staff', 'stage', 'stain', 'stake',
        'stale', 'standard', 'stare', 'starve', 'state', 'statement', 'status',
        'steady', 'steep', 'stereotype', 'stick', 'stiff', 'stimulate', 'sting',
        'stir', 'stock', 'storage', 'straightforward', 'strain', 'stranger',
        'strategy', 'strength', 'stress', 'stretch', 'strict', 'strike',
        'string', 'strip', 'strive', 'stroke', 'structure', 'struggle', 'stuff',
        'stupid', 'style', 'submit', 'substance', 'substantial', 'substitute',
        'succeed', 'success', 'suck', 'suffer', 'sufficient', 'sugar', 'suggest',
        'suit', 'summary', 'summit', 'superb', 'superior', 'supplement', 'supply',
        'support', 'suppose', 'surface', 'surge', 'surplus', 'surrender',
        'surround', 'survey', 'survive', 'suspect', 'suspend', 'sustain',
        'swallow', 'swap', 'swear', 'sweep', 'swing', 'switch', 'symbol',
        'sympathy', 'system', 'tackle', 'talent', 'target', 'taste', 'tax',
        'team', 'technique', 'technology', 'teenager', 'temper', 'tendency',
        'tender', 'tense', 'tent', 'term', 'terminal', 'terror', 'test',
        'text', 'theme', 'theory', 'therapy', 'thereby', 'thirst', 'thorough',
        'though', 'thought', 'threat', 'thrive', 'throat', 'throughout', 'thrust',
        'ticket', 'tide', 'tidy', 'tight', 'time', 'tissue', 'title', 'toast',
        'tolerate', 'tone', 'topic', 'tough', 'trace', 'track', 'tradition',
        'tragic', 'trail', 'transfer', 'transform', 'transition', 'translate',
        'transport', 'trap', 'treasure', 'treat', 'tremble', 'tremendous',
        'trend', 'trial', 'tribe', 'trick', 'trigger', 'trim', 'triumph',
        'trophy', 'tropical', 'trouble', 'truly', 'trust', 'tunnel', 'twist',
        'typical', 'ultimate', 'uncertain', 'undergo', 'underline', 'understand',
        'undertake', 'unfortunately', 'uniform', 'union', 'unique', 'unite',
        'universal', 'universe', 'unlike', 'unlikely', 'unusual', 'update',
        'upgrade', 'urge', 'urgent', 'utility', 'utilize', 'utter', 'vacant',
        'vague', 'valid', 'valley', 'valuable', 'value', 'vanish', 'variable',
        'variation', 'variety', 'various', 'vary', 'vast', 'vehicle', 'venture',
        'verbal', 'verify', 'version', 'vessel', 'veteran', 'via', 'victim',
        'victory', 'view', 'vigorous', 'violate', 'violence', 'virtual',
        'virtue', 'visible', 'vision', 'visual', 'vital', 'vivid', 'vocabulary',
        'volume', 'voluntary', 'volunteer', 'vote', 'voyage', 'wander', 'warn',
        'wealth', 'weapon', 'weather', 'weave', 'welfare', 'whereas', 'while',
        'whisper', 'widen', 'wisdom', 'withdraw', 'witness', 'wonder', 'workshop',
        'world', 'worship', 'worth', 'worthy', 'wound', 'wrap', 'yield', 'zone'
    ],
    'grade_9': [
        'abandon', 'abolish', 'absurd', 'abundant', 'accelerate', 'accommodation',
        'accompany', 'accomplish', 'account', 'accumulate', 'accurate', 'accuse',
        'accustomed', 'achievement', 'acknowledge', 'acquaintance', 'acquire',
        'adaptation', 'adequate', 'adjustment', 'administration', 'adoption',
        'advantage', 'adventure', 'adverse', 'advocate', 'aesthetic', 'affection',
        'aftermath', 'agency', 'agitate', 'agricultural', 'airline', 'allegiance',
        'allocate', 'alteration', 'alternative', 'ambassador', 'ambiguous',
        'ambition', 'amendment', 'ammunition', 'ample', 'analogy', 'analyse',
        'ancestor', 'anecdote', 'anguish', 'anniversary', 'announcement',
        'anonymous', 'anticipate', 'anxiety', 'apparatus', 'appeal', 'appetite',
        'applaud', 'appliance', 'application', 'appoint', 'appreciate', 'approach',
        'appropriate', 'approval', 'approximately', 'archaeology', 'architect',
        'aristocracy', 'armor', 'array', 'arrogant', 'articulate', 'artificial',
        'ascend', 'aspiration', 'assemble', 'assert', 'assess', 'assign',
        'assistance', 'associate', 'assumption', 'assurance', 'astronomy',
        'atmosphere', 'attain', 'attempt', 'attribute', 'auction', 'authentic',
        'authority', 'autonomous', 'auxiliary', 'available', 'avalanche',
        'avert', 'aviation', 'awe', 'backbone', 'bacteria', 'baffle', 'bakery',
        'balance', 'bankrupt', 'banner', 'barrier', 'beforehand', 'behave',
        'belongings', 'beneath', 'beneficial', 'betray', 'bid', 'biography',
        'bitterness', 'blackmail', 'blanket', 'blast', 'blaze', 'bleak',
        'blessing', 'blink', 'blockade', 'blossom', 'blueprint', 'blunder',
        'blunt', 'blur', 'boast', 'bonus', 'boom', 'boycott', 'breed',
        'brisk', 'brittle', 'brochure', 'brutal', 'buckle', 'budget', 'buffer',
        'bulletin', 'bureaucracy', 'burst', 'cabin', 'calculate', 'calamity',
        'campaign', 'candidate', 'capable', 'capacity', 'captivity', 'capture',
        'career', 'cast', 'casualty', 'catalogue', 'catalyst', 'category',
        'cater', 'caution', 'cease', 'celebrity', 'cemetery', 'census',
        'ceremony', 'certainty', 'certificate', 'chancellor', 'channel',
        'chaos', 'characteristic', 'charity', 'charm', 'chase', 'cherish',
        'chronicle', 'circular', 'circumstance', 'citizenship', 'civic',
        'civilian', 'civilization', 'clarify', 'classic', 'classify', 'clearance',
        'client', 'climate', 'climax', 'cluster', 'coalition', 'coarse',
        'coincide', 'collaborate', 'collapse', 'colleague', 'collective',
        'collide', 'collision', 'colonial', 'combat', 'comedy', 'comet',
        'commemorate', 'commence', 'commentary', 'commerce', 'commission',
        'committee', 'commodity', 'commonplace', 'communicate', 'community',
        'companion', 'comparable', 'comparison', 'compartment', 'compassion',
        'compel', 'compensate', 'compete', 'competent', 'complaint', 'complement',
        'complex', 'comply', 'component', 'compose', 'comprehend', 'comprehensive',
        'comprise', 'compulsory', 'conceal', 'concede', 'conceive', 'concentrate',
        'concept', 'concern', 'concise', 'conclude', 'concrete', 'condemn',
        'condition', 'condone', 'conduct', 'confer', 'confession', 'confidence',
        'confidential', 'configuration', 'confirm', 'conflict', 'conform',
        'confront', 'confuse', 'congregate', 'conjunction', 'conquer', 'conscience',
        'conscious', 'consensus', 'consent', 'consequence', 'conservation',
        'considerable', 'consist', 'console', 'consolidate', 'conspicuous',
        'constant', 'constitute', 'constitution', 'construct', 'consult',
        'consume', 'contemplate', 'contemporary', 'contend', 'contest',
        'context', 'continent', 'contingency', 'contradict', 'contradiction',
        'contrary', 'contrast', 'contribute', 'controversial', 'controversy',
        'convenience', 'convention', 'convert', 'convey', 'conviction',
        'convince', 'cooperate', 'coordinate', 'copyright', 'corporate',
        'corps', 'correct', 'correlate', 'correspond', 'corridor', 'corrupt',
        'costume', 'council', 'counterpart', 'courage', 'courteous', 'courtesy',
        'coverage', 'craft', 'crater', 'credit', 'crew', 'crisis', 'criterion',
        'critical', 'criticism', 'critique', 'crucial', 'cruise', 'crumble',
        'crusade', 'cultivate', 'cumulative', 'curiosity', 'currency', 'current',
        'curriculum', 'customary', 'cylinder', 'cynical', 'database', 'deadline',
        'debate', 'decade', 'decay', 'decent', 'decisive', 'declaration',
        'decline', 'decompose', 'decorate', 'decrease', 'dedicate', 'deem',
        'default', 'defect', 'defend', 'deficit', 'define', 'defy', 'delegate',
        'deliberate', 'delicate', 'deliver', 'demand', 'democracy', 'demonstrate',
        'denial', 'denounce', 'dense', 'departure', 'dependent', 'depict',
        'deplete', 'deposit', 'depress', 'deprivation', 'derive', 'descend',
        'describe', 'deserve', 'design', 'desolate', 'despair', 'desperate',
        'destination', 'destiny', 'destruction', 'detach', 'detail', 'detect',
        'deteriorate', 'determine', 'detest', 'develop', 'deviate', 'device',
        'devote', 'diagnose', 'diagram', 'dialect', 'dialogue', 'diameter',
        'dictate', 'dilemma', 'diligent', 'dimension', 'diminish', 'diploma',
        'diplomat', 'direct', 'disability', 'disadvantage', 'disappoint',
        'disaster', 'discipline', 'disclose', 'discount', 'discover', 'discreet',
        'discrepancy', 'discriminate', 'disgrace', 'disguise', 'disgust',
        'dismiss', 'disorder', 'dispatch', 'dispense', 'display', 'dispose',
        'dispute', 'disrupt', 'dissolve', 'distinct', 'distinguish', 'distort',
        'distract', 'distress', 'distribute', 'disturb', 'diverge', 'diverse',
        'diversion', 'divert', 'doctrine', 'document', 'domain', 'domestic',
        'dominant', 'dominate', 'donate', 'dose', 'doubt', 'draft', 'drain',
        'drama', 'drastic', 'drawback', 'dread', 'drift', 'drought', 'dubious',
        'duplicate', 'duration', 'dwell', 'dynamic', 'eager', 'earnest',
        'earthquake', 'ease', 'ecology', 'economy', 'edible', 'edit', 'effect',
        'efficient', 'effort', 'elaborate', 'elect', 'electric', 'electronic',
        'elegant', 'element', 'elevate', 'eligible', 'eliminate', 'elite',
        'eloquent', 'embark', 'embarrass', 'embed', 'embody', 'embrace',
        'emerge', 'emergency', 'emigrate', 'eminent', 'emotion', 'emphasis',
        'employ', 'empower', 'enable', 'encounter', 'encourage', 'endanger',
        'endeavor', 'endure', 'energetic', 'enforce', 'engage', 'enhance',
        'enlighten', 'enormous', 'enrich', 'ensure', 'enterprise', 'entertain',
        'enthusiasm', 'entitle', 'entity', 'entrust', 'environment', 'envision',
        'episode', 'epoch', 'equator', 'equip', 'equivalent', 'era', 'erode',
        'erroneous', 'escape', 'essence', 'essential', 'establish', 'estate',
        'esteem', 'estimate', 'eternal', 'evaluate', 'evaporate', 'eventually',
        'evidence', 'evolve', 'exaggerate', 'exceed', 'excel', 'exception',
        'excessive', 'exchange', 'exclaim', 'exclude', 'execute', 'exempt',
        'exert', 'exhaust', 'exhibit', 'exile', 'existence', 'exit', 'expand',
        'expectation', 'expedition', 'expel', 'expenditure', 'expense',
        'experience', 'experiment', 'expertise', 'expire', 'explicit', 'exploit',
        'explore', 'expose', 'exposure', 'extend', 'extent', 'external',
        'extraordinary', 'extreme', 'fabric', 'facilitate', 'facility', 'factor',
        'faculty', 'fade', 'faithful', 'fame', 'familiar', 'famine', 'fancy',
        'fantasy', 'fascinate', 'fatal', 'fatigue', 'favorable', 'feasible',
        'feature', 'federal', 'feeble', 'fellowship', 'feminine', 'fertile',
        'fierce', 'figure', 'filter', 'finance', 'finite', 'fitting', 'fixture',
        'flame', 'flash', 'flavor', 'flee', 'flexible', 'fling', 'flourish',
        'fluctuate', 'fluent', 'focus', 'foil', 'forecast', 'foremost',
        'forge', 'format', 'formidable', 'formula', 'formulate', 'fortune',
        'forum', 'fossil', 'foster', 'fraction', 'fragment', 'frame', 'friction',
        'fringe', 'frustrate', 'fulfill', 'function', 'fundamental', 'furnish',
        'furthermore', 'gamble', 'garbage', 'gather', 'gauge', 'gaze',
        'generate', 'generous', 'genetic', 'genius', 'genuine', 'gesture',
        'gigantic', 'glimpse', 'glorious', 'glossary', 'govern', 'grace',
        'gradual', 'grant', 'grasp', 'grateful', 'gravity', 'grief', 'grim',
        'grind', 'grip', 'gross', 'guarantee', 'guilt', 'gulf', 'gust',
        'habitat', 'halt', 'hamper', 'handle', 'handsome', 'handy', 'happen',
        'harbor', 'hardship', 'hardware', 'harm', 'harmony', 'harness',
        'harsh', 'haste', 'haunt', 'heal', 'heritage', 'hesitate', 'hierarchy',
        'highlight', 'hike', 'hinder', 'hint', 'horizon', 'horrify', 'hospitality',
        'hostile', 'humanity', 'humble', 'humiliate', 'humor', 'hygiene',
        'hypothesis', 'hysterical', 'identical', 'identify', 'ideology',
        'ignorance', 'illuminate', 'illusion', 'illustrate', 'immense',
        'immerse', 'immigrant', 'immune', 'impact', 'impair', 'implement',
        'implication', 'implicit', 'impose', 'impress', 'impulse', 'inaugurate',
        'incentive', 'incident', 'incline', 'incorporate', 'incredible',
        'incur', 'indefinite', 'indicate', 'indifferent', 'indignant',
        'indispensable', 'induce', 'indulge', 'inevitable', 'infect',
        'infer', 'inflation', 'influence', 'infrastructure', 'ingenious',
        'inhabit', 'inherit', 'inhibit', 'initial', 'initiate', 'inject',
        'innovation', 'inquire', 'insane', 'insight', 'inspect', 'inspire',
        'install', 'instance', 'instant', 'instinct', 'institute', 'instruct',
        'instrument', 'insult', 'insurance', 'intact', 'integrate', 'integrity',
        'intellectual', 'intelligence', 'intense', 'interact', 'interfere',
        'interior', 'intermediate', 'interpret', 'interrupt', 'interval',
        'intervene', 'intimate', 'intimidate', 'intricate', 'intrigue',
        'intrinsic', 'invade', 'investigate', 'involve', 'irony', 'isolate',
        'issue', 'jeopardize', 'journal', 'judgment', 'justification',
        'justify', 'juvenile', 'keen', 'kidnap', 'kindle', 'kindness',
        'knack', 'landscape', 'launch', 'lavish', 'layout', 'leak', 'leap',
        'legacy', 'legitimate', 'leisure', 'lengthy', 'liable', 'liberal',
        'liberty', 'license', 'likelihood', 'limb', 'limitation', 'linear',
        'linger', 'liquid', 'literacy', 'literally', 'literary', 'livelihood',
        'lobby', 'locate', 'locomotive', 'lofty', 'logical', 'loyalty',
        'lucrative', 'luminous', 'luxury', 'magnet', 'magnify', 'maintain',
        'major', 'manifest', 'manipulate', 'mansion', 'manual', 'manufacture',
        'manuscript', 'margin', 'marine', 'masculine', 'massive', 'mature',
        'maximum', 'meadow', 'means', 'mechanism', 'medium', 'melancholy',
        'memorize', 'mental', 'mentor', 'merchant', 'merely', 'merge',
        'metabolism', 'metaphor', 'method', 'meticulous', 'metropolitan',
        'migrate', 'milestone', 'militant', 'millennium', 'mineral', 'minimal',
        'minimum', 'ministry', 'minor', 'minute', 'miracle', 'miserable',
        'mission', 'mob', 'mock', 'modify', 'monarch', 'monitor', 'monopoly',
        'monument', 'moral', 'mortgage', 'motion', 'motivate', 'mould',
        'mourn', 'multiple', 'multitude', 'municipal', 'murder', 'muscle',
        'mutilate', 'mutual', 'mystery', 'myth', 'narrate', 'narrative',
        'nasty', 'nation', 'naughty', 'navigate', 'negative', 'neglect',
        'negotiate', 'neutral', 'nominate', 'nonetheless', 'norm', 'notable',
        'noticeable', 'notion', 'notorious', 'nourish', 'novelty', 'nuance',
        'nuclear', 'nurture', 'nutrition', 'obese', 'object', 'obligation',
        'obscure', 'observation', 'obstacle', 'obstinate', 'obtain', 'occasion',
        'occupation', 'occupy', 'occur', 'odd', 'odds', 'offend', 'offset',
        'ongoing', 'operate', 'opinion', 'opponent', 'oppose', 'opposite',
        'oppress', 'optimistic', 'option', 'oral', 'orbit', 'orientation',
        'origin', 'ornament', 'outbreak', 'outcome', 'outdoor', 'outline',
        'output', 'outstanding', 'overall', 'overcome', 'overlap', 'overlook',
        'overseas', 'overthrow', 'overwhelm', 'oxygen', 'pacify', 'package',
        'pamphlet', 'paradise', 'paradox', 'parallel', 'parcel', 'parliament',
        'partial', 'participate', 'particular', 'partner', 'passage', 'passion',
        'passive', 'passport', 'patience', 'patriot', 'patrol', 'pattern',
        'pave', 'peak', 'penalty', 'pending', 'perceive', 'percentage',
        'perception', 'permanent', 'permeate', 'permit', 'persist', 'personality',
        'perspective', 'persuade', 'phase', 'phenomenon', 'philosophy', 'pier',
        'pilot', 'pioneer', 'plague', 'planet', 'plantation', 'plateau',
        'platform', 'plead', 'pledge', 'plot', 'plunge', 'poetry', 'poison',
        'policy', 'polish', 'pollute', 'ponder', 'popular', 'portable',
        'portion', 'portrait', 'portray', 'postpone', 'potential', 'poverty',
        'practical', 'precaution', 'precede', 'precise', 'predict', 'predominant',
        'preference', 'prejudice', 'preliminary', 'premier', 'premise',
        'premium', 'preparation', 'prescribe', 'preserve', 'pressure',
        'prestige', 'presume', 'pretend', 'prevail', 'prevalent', 'prevent',
        'previous', 'prey', 'primary', 'prime', 'primitive', 'principal',
        'principle', 'prior', 'priority', 'privilege', 'procedure', 'proceed',
        'process', 'proclaim', 'productive', 'profession', 'profile', 'profit',
        'profound', 'progress', 'prohibit', 'project', 'prominent', 'promote',
        'prompt', 'prone', 'proof', 'propaganda', 'property', 'proportion',
        'propose', 'proposition', 'prospect', 'prosperity', 'prosperous',
        'protect', 'protest', 'protocol', 'provide', 'province', 'provoke',
        'prudent', 'public', 'publish', 'pulse', 'punctual', 'punish',
        'purchase', 'pursue', 'qualify', 'quality', 'quantity', 'quarter',
        'quest', 'quiver', 'quota', 'quote', 'radical', 'rage', 'raid',
        'rally', 'random', 'range', 'rank', 'rapidity', 'rare', 'rate',
        'ratio', 'rational', 'raw', 'react', 'readily', 'realistic', 'reality',
        'realize', 'rebel', 'recall', 'receipt', 'receive', 'reception',
        'recipe', 'reckon', 'recognition', 'recommend', 'record', 'recover',
        'recruit', 'recycle', 'reduce', 'refer', 'reflect', 'reform',
        'refuge', 'refund', 'refuse', 'regain', 'regard', 'regime', 'region',
        'register', 'regress', 'regret', 'regular', 'regulate', 'rehabilitate',
        'reign', 'reinforce', 'reject', 'relate', 'relax', 'release',
        'relevant', 'reliable', 'relief', 'relieve', 'relinquish', 'rely',
        'remain', 'remark', 'remedy', 'remind', 'remnant', 'remote', 'remove',
        'render', 'renew', 'renowned', 'rent', 'repair', 'repeat', 'repel',
        'replace', 'replicate', 'represent', 'repress', 'reproduce', 'republic',
        'reputation', 'request', 'require', 'rescue', 'resemble', 'reserve',
        'residence', 'resign', 'resist', 'resolution', 'resolve', 'resource',
        'respect', 'respond', 'response', 'responsibility', 'restore',
        'restrain', 'restrict', 'result', 'retain', 'retire', 'retort',
        'retreat', 'retrieve', 'return', 'reveal', 'revenge', 'revenue',
        'reverse', 'review', 'revise', 'revolution', 'revolve', 'reward',
        'rhythm', 'ridiculous', 'rigid', 'rigorous', 'rival', 'robust',
        'romantic', 'rot', 'rough', 'routine', 'rubbish', 'rude', 'ruin',
        'sacred', 'sacrifice', 'sake', 'salvage', 'salvation', 'sanction',
        'satisfy', 'saturate', 'scale', 'scan', 'scandal', 'scatter',
        'scenario', 'schedule', 'scheme', 'scholar', 'scope', 'scratch',
        'scream', 'scrutiny', 'secure', 'seek', 'seemingly', 'segment',
        'seize', 'sensation', 'sensitive', 'sentence', 'sentiment', 'separate',
        'sequence', 'serene', 'settle', 'severe', 'shabby', 'shatter',
        'shed', 'sheer', 'shelter', 'shift', 'shiver', 'shock', 'shore',
        'shortage', 'shoulder', 'shrewd', 'shrink', 'siege', 'signal',
        'significant', 'silence', 'similar', 'simulate', 'simultaneous',
        'sincere', 'site', 'skeptical', 'slap', 'slash', 'slaughter',
        'slavery', 'slender', 'slice', 'slight', 'slip', 'slope', 'smash',
        'smooth', 'snatch', 'soar', 'so-called', 'sober', 'sociology',
        'solar', 'sole', 'solemn', 'solid', 'solution', 'solve', 'sophisticated',
        'source', 'sovereign', 'space', 'span', 'spark', 'special', 'species',
        'specific', 'specification', 'spectacular', 'spectator', 'spectrum',
        'speculate', 'sphere', 'spill', 'spin', 'spiral', 'spiritual',
        'spite', 'split', 'spoil', 'sponsor', 'spontaneous', 'spread',
        'stable', 'stagger', 'stain', 'stake', 'stale', 'stall', 'standard',
        'stare', 'starve', 'statement', 'statesman', 'static', 'stationary',
        'statistic', 'status', 'statute', 'steady', 'steep', 'stereotype',
        'sterile', 'stick', 'stiff', 'stimulate', 'sting', 'stir', 'stock',
        'storage', 'straightforward', 'strain', 'strand', 'strap', 'strategy',
        'strengthen', 'stress', 'stretch', 'strict', 'stride', 'strike',
        'string', 'strip', 'strive', 'stroke', 'structure', 'struggle',
        'stubborn', 'stuff', 'stumble', 'stun', 'style', 'submit', 'subordinate',
        'subscribe', 'subsequent', 'substance', 'substantial', 'substitute',
        'subtle', 'succeed', 'successor', 'sue', 'suffice', 'suffer',
        'sufficient', 'suggest', 'suite', 'summary', 'summit', 'summon',
        'superb', 'superficial', 'superior', 'supplement', 'supply', 'support',
        'suppose', 'suppress', 'supreme', 'surface', 'surge', 'surgeon',
        'surplus', 'surrender', 'surround', 'survey', 'survival', 'survive',
        'suspect', 'suspend', 'suspicion', 'sustain', 'swallow', 'swamp',
        'swap', 'swear', 'swell', 'swing', 'switch', 'symbol', 'sympathetic',
        'sympathy', 'symptom', 'syndrome', 'synthesis', 'system', 'tackle',
        'tactic', 'talent', 'tame', 'target', 'tariff', 'teem', 'temper',
        'tempo', 'temporary', 'tempt', 'tend', 'tendency', 'tender', 'tense',
        'tentative', 'terminal', 'terminate', 'terrace', 'terrain', 'terrify',
        'territory', 'testimony', 'texture', 'theme', 'theory', 'therapy',
        'thereby', 'thermal', 'thesis', 'thirst', 'thorough', 'threat',
        'threshold', 'thrive', 'throat', 'thrust', 'tick', 'tide', 'tidy',
        'tilt', 'timber', 'timing', 'timid', 'tissue', 'tolerate', 'tone',
        'topic', 'torment', 'tough', 'toxic', 'trace', 'track', 'tradition',
        'tragic', 'trail', 'trait', 'transaction', 'transcend', 'transfer',
        'transform', 'transition', 'translate', 'transmission', 'transparent',
        'transplant', 'transport', 'trap', 'treason', 'treasure', 'treaty',
        'tremble', 'tremendous', 'trend', 'trial', 'tribe', 'tribute',
        'trigger', 'trillion', 'trim', 'triple', 'triumph', 'trivial',
        'tropical', 'trouble', 'troublesome', 'truism', 'truly', 'trumpet',
        'trust', 'tunnel', 'turbulence', 'turnover', 'twilight', 'twist',
        'typical', 'tyranny', 'ultimate', 'unanimous', 'unbearable', 'uncertainty',
        'uncover', 'undergo', 'underline', 'undermine', 'undertake', 'unemployed',
        'unexpected', 'unfair', 'unfortunate', 'uniform', 'unique', 'unite',
        'universal', 'universe', 'unprecedented', 'unpredictable', 'unquestionable',
        'unreliable', 'unsettle', 'unsustainable', 'unveil', 'upcoming',
        'update', 'upgrade', 'upheaval', 'uphold', 'urge', 'urgent', 'utility',
        'utilize', 'utmost', 'utter', 'vacant', 'vacuum', 'vague', 'valid',
        'valley', 'valuable', 'value', 'vanish', 'variable', 'variation',
        'variety', 'various', 'vary', 'vast', 'vehicle', 'venture', 'verbal',
        'verdict', 'verify', 'versatile', 'version', 'vertical', 'vessel',
        'veteran', 'veto', 'via', 'vibrant', 'vice', 'victim', 'victory',
        'view', 'vigorous', 'violate', 'violence', 'virtual', 'virtue',
        'visible', 'vision', 'visual', 'vital', 'vivid', 'vocal', 'void',
        'volume', 'voluntary', 'volunteer', 'vote', 'voyage', 'vulnerable',
        'wander', 'ward', 'warehouse', 'warfare', 'warrant', 'warranty',
        'warrior', 'wary', 'watershed', 'weaken', 'wealth', 'weapon',
        'weary', 'welfare', 'whereas', 'whisper', 'widen', 'widespread',
        'wisdom', 'withdraw', 'witness', 'wonder', 'workshop', 'world',
        'worship', 'worth', 'worthy', 'wound', 'wrap', 'yearn', 'yield', 'zone'
    ]
}

def get_grade_vocab(grade):
    """Get grade vocabulary, initialize if not exists"""
    vocab = cos.get_json('config/grade_vocab.json')
    if vocab is None:
        vocab = BASIC_VOCAB
        cos.put_json('config/grade_vocab.json', vocab)
    grade_key = f'grade_{grade}'
    return vocab.get(grade_key, vocab.get('grade_7', []))

# ==================== Structured Vocabulary ====================
GRADE_INFO = {
    6: {'name': '6年级', 'units_desc': '小学六年级词汇，按主题分类'},
    7: {'name': '7年级', 'units_desc': '人教版7年级Go for it教材，上册9单元+下册12单元'},
    8: {'name': '8年级', 'units_desc': '人教版8年级Go for it教材，上册10单元+下册10单元'},
    9: {'name': '9年级', 'units_desc': '人教版9年级Go for it全一册，14单元'},
}

def init_structured_vocab(grade):
    """Call DeepSeek to organize flat vocab into unit/lesson structure based on textbook"""
    flat_vocab = get_grade_vocab(grade)
    if not flat_vocab:
        return None

    grade_info = GRADE_INFO.get(grade, GRADE_INFO[7])
    prompt = (
        f'你是初中英语教材专家，熟悉人教版Go for it教材。'
        f'请将以下{grade_info["name"]}英语单词按照人教版Go for it教材的单元结构进行分类。\n\n'
        f'教材结构参考：{grade_info["units_desc"]}\n\n'
        '要求：\n'
        '1. 每个单元分2课：Section A 和 Section B\n'
        '2. 根据单词的难度和主题，归入最合适的单元和课\n'
        '3. 每个单词只能归入一个课，确保所有单词都被分配\n'
        '4. 单元名格式：Unit N - 主题描述（英文）\n\n'
        '返回JSON格式（严格遵循）：\n'
        '{"units":[{"unit_id":1,"unit_name":"Unit 1 - My name\'s Gina",'
        '"lessons":[{"lesson_id":1,"lesson_name":"Section A","words":["hello","name"]},'
        '{"lesson_id":2,"lesson_name":"Section B","words":["nice","meet"]}]}]}\n\n'
        '只返回JSON，不要其他内容。\n'
        f'{grade_info["name"]}单词列表: {json.dumps(flat_vocab, ensure_ascii=False)}'
    )
    result = deepseek(prompt, timeout=90, max_tokens=8192, call_type='init_vocab')
    if result is None:
        return None

    parsed = extract_json(result)
    if parsed and isinstance(parsed, dict) and 'units' in parsed:
        return parsed
    return None

def get_structured_vocab(grade):
    """Get structured vocab from COS, initialize if not exists"""
    structured = cos.get_json('config/grade_vocab_structured.json')
    if structured is None:
        structured = {}
    grade_key = f'grade_{grade}'
    if grade_key not in structured:
        # Try to initialize via AI
        result = init_structured_vocab(grade)
        if result:
            structured[grade_key] = result
            cos.put_json('config/grade_vocab_structured.json', structured)
        else:
            return None
    return structured.get(grade_key)

def get_words_from_lesson(grade, unit_id, lesson_id):
    """Get words for a specific lesson"""
    structured = get_structured_vocab(grade)
    if not structured:
        return []
    for unit in structured.get('units', []):
        if unit.get('unit_id') == unit_id:
            for lesson in unit.get('lessons', []):
                if lesson.get('lesson_id') == lesson_id:
                    return lesson.get('words', [])
    return []

def get_words_from_unit(grade, unit_id):
    """Get all words for a specific unit"""
    structured = get_structured_vocab(grade)
    if not structured:
        return []
    for unit in structured.get('units', []):
        if unit.get('unit_id') == unit_id:
            words = []
            for lesson in unit.get('lessons', []):
                words.extend(lesson.get('words', []))
            return words
    return []

def _find_static(filename):
    """Find a static file across possible SCF runtime directories."""
    candidates = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), filename),
        os.path.join(os.getcwd(), filename),
        os.path.join('/var/task', filename),
        os.path.join('/mnt/auto', filename),
        filename,
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    return None

# ==================== Routes: Static ====================
@app.route('/')
def index():
    html_path = _find_static('index.html')
    if html_path:
        with open(html_path, 'r', encoding='utf-8') as f:
            return Response(f.read(), mimetype='text/html')
    return 'index.html not found', 404

# ==================== Routes: Auth ====================
@app.route('/api/login', methods=['POST'])
def login():
    try:
        data = request.get_json() or {}
        role = data.get('role')
        password = data.get('password', '')
        family_id = data.get('family_id')

        if role == 'admin':
            if hash_pwd(password) == ADMIN_PWD_VERIFY:
                return ok({'role': 'admin', 'token': ADMIN_PWD_VERIFY[:16]})
            return fail('密码错误')

        if not family_id:
            return fail('缺少家庭标识')

        users = get_users(family_id)
        if not users:
            return fail('家庭不存在')

        if role == 'parent':
            parent = users.get('parent', {})
            if hash_pwd(password) == parent.get('password_hash'):
                return ok({
                    'role': 'parent', 'family_id': family_id,
                    'family_name': users.get('family_name', ''),
                    'parent_name': parent.get('name', '家长')
                })
            return fail('密码错误')

        if role == 'child':
            for child in users.get('children', []):
                if hash_pwd(password) == child.get('password_hash'):
                    return ok({
                        'role': 'child', 'family_id': family_id,
                        'child_id': child['child_id'],
                        'child_name': child['name'],
                        'grade': child.get('grade', 7)
                    })
            return fail('密码错误')

        return fail('未知角色')
    except Exception as e:
        return fail(f'登录失败: {e}')

@app.route('/api/change-password', methods=['POST'])
def change_password():
    try:
        data = request.get_json() or {}
        family_id = data.get('family_id')
        old_pwd = data.get('old_password', '')
        new_pwd = data.get('new_password', '')
        user_type = data.get('user_type', 'parent')  # parent / child
        child_id = data.get('child_id')

        if not family_id or not new_pwd:
            return fail('参数缺失')

        users = get_users(family_id)
        if not users:
            return fail('家庭不存在')

        if user_type == 'parent':
            parent = users.get('parent', {})
            if hash_pwd(old_pwd) != parent.get('password_hash'):
                return fail('原密码错误')
            new_hash = hash_pwd(new_pwd)
            # Check global uniqueness for parent passwords
            if not check_parent_pwd_global(new_hash, exclude_family=family_id):
                return fail('该密码已被其他家庭使用，请更换')
            # Check uniqueness within family
            for ch in users.get('children', []):
                if ch.get('password_hash') == new_hash:
                    return fail('该密码与孩子密码重复，请更换')
            parent['password_hash'] = new_hash
            users['parent'] = parent
        else:
            if not child_id:
                return fail('缺少孩子ID')
            child = None
            for ch in users.get('children', []):
                if ch['child_id'] == child_id:
                    child = ch
                    break
            if not child:
                return fail('孩子不存在')
            if hash_pwd(old_pwd) != child.get('password_hash'):
                return fail('原密码错误')
            new_hash = hash_pwd(new_pwd)
            # Check uniqueness within family
            if users.get('parent', {}).get('password_hash') == new_hash:
                return fail('该密码与家长密码重复，请更换')
            for ch in users.get('children', []):
                if ch.get('child_id') != child_id and ch.get('password_hash') == new_hash:
                    return fail('该密码已被本家庭其他孩子使用，请更换')
            child['password_hash'] = new_hash

        save_users(family_id, users)
        return ok(msg='密码修改成功')
    except Exception as e:
        return fail(f'修改失败: {e}')

# ==================== Routes: Admin ====================
@app.route('/api/families', methods=['GET', 'POST'])
def families_route():
    try:
        if request.method == 'GET':
            return ok(get_families())

        data = request.get_json() or {}
        family_name = data.get('family_name', '')
        parent_name = data.get('parent_name', '家长')
        password = data.get('password', '')

        if not family_name or not password:
            return fail('家庭名称和密码不能为空')

        new_hash = hash_pwd(password)
        if not check_parent_pwd_global(new_hash):
            return fail('该密码已被其他家庭使用，请更换')

        family_id = gen_id()
        families = get_families()
        families.append({
            'family_id': family_id,
            'family_name': family_name,
            'created_at': int(time.time())
        })
        save_families(families)

        users = {
            'family_name': family_name,
            'parent': {
                'name': parent_name,
                'password_hash': new_hash,
                'created_at': int(time.time())
            },
            'children': []
        }
        if not save_users(family_id, users):
            return fail('数据保存失败，请检查COS配置')
        return ok({'family_id': family_id}, '家庭创建成功')
    except Exception as e:
        return fail(f'操作失败: {e}')

@app.route('/api/config', methods=['GET', 'POST'])
def config_route():
    try:
        if request.method == 'GET':
            return ok(get_config())
        data = request.get_json() or {}
        cfg = get_config()
        cfg.update(data)
        cos.put_json('config/system.json', cfg)
        return ok(cfg, '配置已更新')
    except Exception as e:
        return fail(f'操作失败: {e}')

@app.route('/api/usage', methods=['GET'])
def usage_route():
    try:
        return ok(get_usage_stats())
    except Exception as e:
        return fail(f'操作失败: {e}')

# ==================== Routes: Children ====================
@app.route('/api/children', methods=['GET', 'POST'])
def children_route():
    try:
        family_id = request.args.get('family_id') or (request.get_json() or {}).get('family_id')
        if not family_id:
            return fail('缺少家庭标识')

        users = get_users(family_id)
        if not users:
            return fail('家庭不存在')

        if request.method == 'GET':
            children = users.get('children', [])
            # Strip password hashes
            safe = [{'child_id': c['child_id'], 'name': c['name'],
                     'grade': c.get('grade', 7), 'created_at': c.get('created_at', 0)}
                    for c in children]
            return ok(safe)

        data = request.get_json() or {}
        name = data.get('name', '')
        grade = data.get('grade', 7)
        password = data.get('password', '')

        if not name or not password:
            return fail('姓名和密码不能为空')

        new_hash = hash_pwd(password)
        # Check uniqueness within family
        if users.get('parent', {}).get('password_hash') == new_hash:
            return fail('该密码与家长密码重复')
        for ch in users.get('children', []):
            if ch.get('password_hash') == new_hash:
                return fail('该密码已被其他孩子使用')

        child = {
            'child_id': gen_id(),
            'name': name,
            'grade': grade,
            'password_hash': new_hash,
            'created_at': int(time.time())
        }
        users.setdefault('children', []).append(child)
        save_users(family_id, users)

        return ok({'child_id': child['child_id'], 'name': name, 'grade': grade}, '孩子添加成功')
    except Exception as e:
        return fail(f'操作失败: {e}')

@app.route('/api/children/<child_id>', methods=['PUT', 'DELETE'])
def child_detail(child_id):
    try:
        family_id = request.args.get('family_id') or (request.get_json() or {}).get('family_id')
        if not family_id:
            return fail('缺少家庭标识')
        users = get_users(family_id)
        if not users:
            return fail('家庭不存在')

        children = users.get('children', [])
        idx = None
        for i, c in enumerate(children):
            if c['child_id'] == child_id:
                idx = i
                break
        if idx is None:
            return fail('孩子不存在')

        if request.method == 'DELETE':
            users['children'].pop(idx)
            save_users(family_id, users)
            return ok(msg='已删除')

        data = request.get_json() or {}
        if 'name' in data:
            children[idx]['name'] = data['name']
        if 'grade' in data:
            children[idx]['grade'] = data['grade']
        if 'password' in data:
            new_hash = hash_pwd(data['password'])
            if users.get('parent', {}).get('password_hash') == new_hash:
                return fail('该密码与家长密码重复')
            for ch in children:
                if ch['child_id'] != child_id and ch.get('password_hash') == new_hash:
                    return fail('该密码已被其他孩子使用')
            children[idx]['password_hash'] = new_hash
        save_users(family_id, users)
        return ok(msg='已更新')
    except Exception as e:
        return fail(f'操作失败: {e}')

# ==================== Routes: Entry Code ====================
@app.route('/api/entry-code', methods=['POST'])
def gen_entry_code():
    try:
        data = request.get_json() or {}
        family_id = data.get('family_id')
        child_id = data.get('child_id')
        if not family_id or not child_id:
            return fail('参数缺失')

        users = get_users(family_id)
        if not users:
            return fail('家庭不存在')
        child = None
        for c in users.get('children', []):
            if c['child_id'] == child_id:
                child = c
                break
        if not child:
            return fail('孩子不存在')

        # Generate unique 4-digit code
        for _ in range(10):
            code = gen_code()
            if get_session(code) is None:
                break
        else:
            return fail('生成录入码失败，请重试')

        session = {
            'code': code,
            'family_id': family_id,
            'child_id': child_id,
            'child_name': child['name'],
            'grade': child.get('grade', 7),
            'unit_id': None,
            'created_at': int(time.time()),
            'expires_at': int(time.time()) + 28800,  # 8 hours
            'status': 'active',
            'last_active': int(time.time())
        }
        save_session(code, session)
        return ok({'code': code, 'expires_at': session['expires_at']}, '录入码已生成')
    except Exception as e:
        return fail(f'生成失败: {e}')

@app.route('/api/entry-code/cancel', methods=['POST'])
def cancel_entry_code():
    try:
        data = request.get_json() or {}
        code = data.get('code')
        if not code:
            return fail('缺少录入码')
        session = get_session(code)
        if session:
            session['status'] = 'cancelled'
            save_session(code, session)
        return ok(msg='已作废')
    except Exception as e:
        return fail(f'操作失败: {e}')

@app.route('/api/entry-codes', methods=['GET'])
def list_entry_codes():
    try:
        family_id = request.args.get('family_id')
        if not family_id:
            return ok([])
        # We don't have a list API for sessions, so check recent codes
        # This is a limitation - for now return empty if no family_id
        return ok([])
    except Exception as e:
        return fail(f'操作失败: {e}')

# ==================== Routes: Session (Child Entry) ====================
@app.route('/api/session/validate', methods=['POST'])
def validate_session():
    try:
        data = request.get_json() or {}
        code = data.get('code')
        if not code:
            return fail('缺少录入码')

        session = get_session(code)
        if not session:
            return fail('录入码不存在')
        if session['status'] != 'active':
            return fail('录入码已失效')
        if int(time.time()) > session['expires_at']:
            session['status'] = 'expired'
            save_session(code, session)
            return fail('录入码已过期')

        session['last_active'] = int(time.time())
        save_session(code, session)

        # Get existing unit if exists
        unit_data = None
        if session.get('unit_id'):
            units = get_units(session['family_id'])
            for u in units:
                if u['unit_id'] == session['unit_id']:
                    unit_data = u
                    break

        return ok({
            'code': code,
            'family_id': session['family_id'],
            'child_id': session['child_id'],
            'child_name': session['child_name'],
            'grade': session.get('grade', 7),
            'unit_id': session.get('unit_id'),
            'unit': unit_data,
            'expires_at': session['expires_at']
        })
    except Exception as e:
        return fail(f'验证失败: {e}')

@app.route('/api/session/save', methods=['POST'])
def save_session_draft():
    try:
        data = request.get_json() or {}
        code = data.get('code')
        words = data.get('words', [])
        title = data.get('title', '')

        session = get_session(code)
        if not session or session['status'] != 'active':
            return fail('录入码无效或已失效')
        if int(time.time()) > session['expires_at']:
            return fail('录入码已过期')

        family_id = session['family_id']
        units = get_units(family_id)

        if session.get('unit_id'):
            # Update existing unit
            for u in units:
                if u['unit_id'] == session['unit_id']:
                    u['words'] = words
                    if title:
                        u['title'] = title
                    u['status'] = 'draft'
                    u['updated_at'] = int(time.time())
                    break
        else:
            # Create new unit
            unit_id = gen_id()
            unit = {
                'unit_id': unit_id,
                'title': title or f'录入-{session["child_name"]}-{time.strftime("%m-%d")}',
                'type': 'manual',
                'grade': session.get('grade', 7),
                'status': 'draft',
                'created_at': int(time.time()),
                'created_by': f'child:{session["child_id"]}',
                'child_name': session['child_name'],
                'words': words
            }
            units.append(unit)
            session['unit_id'] = unit_id

        save_units(family_id, units)
        session['last_active'] = int(time.time())
        save_session(code, session)
        return ok({'unit_id': session['unit_id']}, '草稿已保存')
    except Exception as e:
        return fail(f'保存失败: {e}')

@app.route('/api/session/submit', methods=['POST'])
def submit_session():
    try:
        data = request.get_json() or {}
        code = data.get('code')

        session = get_session(code)
        if not session or session['status'] != 'active':
            return fail('录入码无效或已失效')

        family_id = session['family_id']
        units = get_units(family_id)

        if session.get('unit_id'):
            for u in units:
                if u['unit_id'] == session['unit_id']:
                    u['status'] = 'pending'
                    u['submitted_at'] = int(time.time())
                    break
            save_units(family_id, units)

        session['status'] = 'submitted'
        save_session(code, session)
        # Don't delete session immediately - keep for audit, will expire
        return ok({'unit_id': session.get('unit_id')}, '单词已提交')
    except Exception as e:
        return fail(f'提交失败: {e}')

@app.route('/api/session/release', methods=['POST'])
def release_session():
    try:
        data = request.get_json() or {}
        code = data.get('code')
        session = get_session(code)
        if session and session['status'] == 'active':
            # Mark as released but keep valid for re-entry within 8h
            session['last_active'] = 0
            save_session(code, session)
        return ok()
    except Exception:
        return ok()

# ==================== Routes: Units ====================
@app.route('/api/units', methods=['GET', 'POST'])
def units_route():
    try:
        family_id = request.args.get('family_id') or (request.get_json() or {}).get('family_id')
        if not family_id:
            return fail('缺少家庭标识')

        if request.method == 'GET':
            units = get_units(family_id)
            # Return summary (without full word details for list)
            summary = [{
                'unit_id': u['unit_id'], 'title': u['title'],
                'type': u.get('type', 'manual'),
                'grade': u.get('grade', 7),
                'status': u.get('status', 'pending'),
                'word_count': len(u.get('words', [])),
                'created_at': u.get('created_at', 0),
                'child_name': u.get('child_name', '')
            } for u in units]
            return ok(summary)

        data = request.get_json() or {}
        unit = {
            'unit_id': gen_id(),
            'title': data.get('title', f'单元-{time.strftime("%m-%d")}'  ),
            'type': data.get('type', 'manual'),
            'grade': data.get('grade', 7),
            'status': data.get('status', 'pending'),
            'created_at': int(time.time()),
            'created_by': data.get('created_by', 'parent'),
            'child_name': data.get('child_name', ''),
            'words': data.get('words', [])
        }
        units = get_units(family_id)
        units.append(unit)
        save_units(family_id, units)
        return ok(unit, '单元已创建')
    except Exception as e:
        return fail(f'操作失败: {e}')

@app.route('/api/units/<unit_id>', methods=['GET', 'PUT', 'DELETE'])
def unit_detail(unit_id):
    try:
        family_id = request.args.get('family_id') or (request.get_json() or {}).get('family_id')
        if not family_id:
            return fail('缺少家庭标识')
        units = get_units(family_id)
        idx = None
        for i, u in enumerate(units):
            if u['unit_id'] == unit_id:
                idx = i
                break
        if idx is None:
            return fail('单元不存在')

        if request.method == 'GET':
            return ok(units[idx])

        if request.method == 'DELETE':
            units.pop(idx)
            save_units(family_id, units)
            return ok(msg='已删除')

        data = request.get_json() or {}
        for k in ['title', 'status', 'words', 'grade', 'type']:
            if k in data:
                units[idx][k] = data[k]
        units[idx]['updated_at'] = int(time.time())
        save_units(family_id, units)
        return ok(units[idx], '已更新')
    except Exception as e:
        return fail(f'操作失败: {e}')

# ==================== Routes: AI ====================
@app.route('/api/ai/check', methods=['POST'])
def ai_check():
    try:
        data = request.get_json() or {}
        words = data.get('words', [])
        if not words:
            return fail('没有需要检查的单词')

        prompt = (
            '请检查以下英文单词的拼写是否正确。返回JSON数组，每个元素包含：\n'
            '- word: 原始输入\n'
            '- valid: 是否拼写正确(true/false)\n'
            '- suggestion: 如果错误，给出正确拼写\n'
            '只返回JSON，不要其他内容。\n'
            f'单词列表: {json.dumps(words, ensure_ascii=False)}'
        )
        result = deepseek(prompt, call_type='check')
        if result is None:
            return ok([{'word': w, 'valid': True} for w in words], 'AI服务不可用，已跳过校验')

        parsed = extract_json(result)
        if parsed and isinstance(parsed, list):
            return ok(parsed)
        return fail('AI返回格式异常')
    except Exception as e:
        return fail(f'校验失败: {e}')

@app.route('/api/ai/generate', methods=['POST'])
def ai_generate():
    try:
        data = request.get_json() or {}
        words = data.get('words', [])
        if not words:
            return fail('没有需要生成内容的单词')

        prompt = (
            '请为以下英文单词生成音标、中文释义和例句。要求：\n'
            '1. 音标使用IPA标准格式\n'
            '2. 中文释义简洁，适合初中生，包含词性\n'
            '3. 例句不超过15个词，使用初中考纲词汇\n'
            '返回JSON数组：\n'
            '[{"word":"apple","phonetic":"/aepl/","translation":"n. 苹果","sentence":"I eat an apple every day."}]\n'
            '只返回JSON，不要其他内容。\n'
            f'单词列表: {json.dumps(words, ensure_ascii=False)}'
        )
        result = deepseek(prompt, timeout=60, call_type='generate')
        if result is None:
            return fail('AI服务不可用，请检查API Key配置')

        parsed = extract_json(result)
        if parsed and isinstance(parsed, list):
            return ok(parsed)
        return fail('AI返回格式异常')
    except Exception as e:
        return fail(f'生成失败: {e}')

@app.route('/api/ai/training', methods=['POST'])
def ai_training():
    try:
        data = request.get_json() or {}
        grade = data.get('grade', 7)
        count = min(max(data.get('count', 5), 1), 50)
        unit_id = data.get('unit_id')
        lesson_ids = data.get('lesson_ids', [])

        # Determine word source
        if lesson_ids:
            # Specific lessons selected
            selected = []
            for lid in lesson_ids:
                selected.extend(get_words_from_lesson(grade, unit_id, lid))
            # Remove duplicates while preserving order
            seen = set()
            selected = [w for w in selected if not (w in seen or seen.add(w))]
            # Random sample if more than count
            if len(selected) > count:
                selected = random.sample(selected, count)
        elif unit_id:
            # Entire unit selected
            selected = get_words_from_unit(grade, unit_id)
            seen = set()
            selected = [w for w in selected if not (w in seen or seen.add(w))]
            if len(selected) > count:
                selected = random.sample(selected, count)
        else:
            # Random from entire grade (backward compatible)
            vocab = get_grade_vocab(grade)
            if len(vocab) >= count:
                selected = random.sample(vocab, count)
            else:
                selected = vocab[:]

        if not selected:
            return fail('未找到单词，可能词库尚未结构化，请先初始化')

        # Build title
        title_parts = [f'暑期特训-{time.strftime("%Y-%m-%d")}-{grade}年级']
        if unit_id:
            structured = get_structured_vocab(grade)
            if structured:
                for u in structured.get('units', []):
                    if u.get('unit_id') == unit_id:
                        title_parts.append(u.get('unit_name', f'Unit {unit_id}'))
                        if lesson_ids:
                            lesson_names = []
                            for l in u.get('lessons', []):
                                if l.get('lesson_id') in lesson_ids:
                                    lesson_names.append(l.get('lesson_name', ''))
                            if lesson_names:
                                title_parts.append('+'.join(lesson_names))
                        break
        title = ' - '.join(title_parts)

        # Generate content for selected words
        prompt = (
            f'请为以下英文单词生成音标、中文释义和例句。要求：\n'
            '1. 音标使用IPA标准格式\n'
            '2. 中文释义简洁，适合初中生，包含词性\n'
            '3. 例句不超过15个词，使用初中考纲词汇\n'
            '返回JSON数组：\n'
            '[{"word":"apple","phonetic":"/aepl/","translation":"n. 苹果","sentence":"I eat an apple every day."}]\n'
            '只返回JSON，不要其他内容。\n'
            f'单词列表: {json.dumps(selected, ensure_ascii=False)}'
        )
        result = deepseek(prompt, timeout=60, call_type='training')
        if result is None:
            return fail('AI服务不可用')

        parsed = extract_json(result)
        if parsed and isinstance(parsed, list):
            return ok({
                'words': parsed,
                'title': title,
                'grade': grade
            })
        return fail('AI返回格式异常')
    except Exception as e:
        return fail(f'生成失败: {e}')

# ==================== Routes: Records ====================
@app.route('/api/records', methods=['GET', 'POST'])
def records_route():
    try:
        family_id = request.args.get('family_id') or (request.get_json() or {}).get('family_id')
        if not family_id:
            return fail('缺少家庭标识')

        if request.method == 'GET':
            child_id = request.args.get('child_id')
            records = get_records(family_id)
            if child_id:
                records = [r for r in records if r.get('child_id') == child_id]
            return ok(records)

        data = request.get_json() or {}
        total = data.get('total', 0)
        correct = data.get('correct_count', 0)
        percentage = round(correct / total * 100, 2) if total > 0 else 0

        record = {
            'record_id': gen_id(),
            'child_id': data.get('child_id'),
            'child_name': data.get('child_name', ''),
            'unit_ids': data.get('unit_ids', []),
            'dictation_order': data.get('dictation_order', []),
            'words': data.get('words', []),
            'correct_count': correct,
            'total': total,
            'percentage': percentage,
            'mode': data.get('mode', 'blind'),
            'created_at': int(time.time())
        }
        records = get_records(family_id)
        records.append(record)
        save_records(family_id, records)
        return ok(record, '成绩已保存')
    except Exception as e:
        return fail(f'操作失败: {e}')

@app.route('/api/records/<record_id>', methods=['DELETE'])
def record_detail(record_id):
    try:
        family_id = request.args.get('family_id')
        if not family_id:
            return fail('缺少家庭标识')
        records = get_records(family_id)
        records = [r for r in records if r['record_id'] != record_id]
        save_records(family_id, records)
        return ok(msg='已删除')
    except Exception as e:
        return fail(f'操作失败: {e}')

# ==================== Routes: Vocab ====================
@app.route('/api/vocab/<int:grade>', methods=['GET'])
def vocab_route(grade):
    try:
        return ok({'grade': grade, 'count': len(get_grade_vocab(grade)), 'words': get_grade_vocab(grade)[:50]})
    except Exception as e:
        return fail(f'操作失败: {e}')

@app.route('/api/vocab/structure', methods=['GET'])
def vocab_structure_route():
    """Get hierarchical vocab structure: grade -> units -> lessons"""
    try:
        grade = int(request.args.get('grade', 7))
        structured = get_structured_vocab(grade)
        if structured is None:
            return fail('词库结构化失败，请检查AI服务配置或稍后重试')
        # Return summary (without full word lists for lighter payload)
        units_summary = []
        for unit in structured.get('units', []):
            lessons_summary = []
            for lesson in unit.get('lessons', []):
                lessons_summary.append({
                    'lesson_id': lesson.get('lesson_id'),
                    'lesson_name': lesson.get('lesson_name', ''),
                    'word_count': len(lesson.get('words', []))
                })
            total_words = sum(l['word_count'] for l in lessons_summary)
            units_summary.append({
                'unit_id': unit.get('unit_id'),
                'unit_name': unit.get('unit_name', ''),
                'lesson_count': len(lessons_summary),
                'word_count': total_words,
                'lessons': lessons_summary
            })
        return ok({'grade': grade, 'units': units_summary})
    except Exception as e:
        return fail(f'操作失败: {e}')

@app.route('/api/vocab/init-structure', methods=['POST'])
def vocab_init_structure_route():
    """Force re-initialize structured vocab for a grade"""
    try:
        data = request.get_json() or {}
        grade = int(data.get('grade', 7))
        result = init_structured_vocab(grade)
        if result is None:
            return fail('AI服务不可用或返回格式异常')
        # Save to COS
        structured = cos.get_json('config/grade_vocab_structured.json')
        if structured is None:
            structured = {}
        structured[f'grade_{grade}'] = result
        cos.put_json('config/grade_vocab_structured.json', structured)
        unit_count = len(result.get('units', []))
        return ok({'grade': grade, 'unit_count': unit_count}, f'词库结构化完成，共{unit_count}个单元')
    except Exception as e:
        return fail(f'操作失败: {e}')

# ==================== SCF Entry Point ====================
def main_handler(event, context):
    return app(event, context)

# ==================== Debug ====================
@app.route('/api/debug/cos')
def debug_cos():
    """Diagnose COS configuration and test read/write"""
    import os
    info = {
        'COS_BUCKET': os.environ.get('COS_BUCKET', '(not set, default: kb-efm-analytics)'),
        'COS_REGION': os.environ.get('COS_REGION', '(not set, default: ap-guangzhou)'),
        'COS_HOST': COS_HOST,
        'COS_PREFIX': COS_PREFIX,
        'COS_SID': COS_SID[:8] + '...' if COS_SID else '(empty)',
        'COS_SKEY_set': bool(COS_SKEY),
    }
    # Test write
    test_key = 'debug_test.json'
    test_data = {'test': True, 'time': int(time.time())}
    w_status, w_body = cos._req('PUT', test_key, test_data)
    info['write_status'] = w_status
    if w_status != 200:
        info['write_error'] = w_body.decode('utf-8')[:500] if w_body else '(empty)'
    else:
        # Test read back
        r_status, r_body = cos._req('GET', test_key)
        info['read_status'] = r_status
        info['read_body'] = r_body.decode('utf-8')[:200] if r_body else '(empty)'
        # Cleanup
        cos._req('DELETE', test_key)
    return jsonify(info)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=9000, debug=False)
