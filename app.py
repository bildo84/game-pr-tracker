from flask import Flask, render_template, request, jsonify, send_file, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta
import requests
import feedparser
from textblob import TextBlob
import json
import os
from io import BytesIO, StringIO
import csv
from apscheduler.schedulers.background import BackgroundScheduler
import logging
import atexit
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import wraps

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-key-please-change')

# Use PostgreSQL if DATABASE_URL is set
database_url = os.getenv('DATABASE_URL')
if database_url:
    if database_url.startswith('postgres://'):
        database_url = database_url.replace('postgres://', 'postgresql://', 1)
    app.config['SQLALCHEMY_DATABASE_URI'] = database_url
else:
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///pr_tracker.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==================== AUTHENTICATION (optional) ====================
AUTH_USERNAME = os.getenv('AUTH_USERNAME', 'admin')
AUTH_PASSWORD = os.getenv('AUTH_PASSWORD', 'changeme123')

def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or auth.username != AUTH_USERNAME or auth.password != AUTH_PASSWORD:
            return jsonify({'error': 'Authentication required'}), 401, {
                'WWW-Authenticate': 'Basic realm="Game PR Tracker"'
            }
        return f(*args, **kwargs)
    return decorated

# ==================== MODELS ====================

class Game(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False, unique=True)
    publisher = db.Column(db.String(200))
    platforms = db.Column(db.String(200))
    keywords = db.Column(db.Text)       # JSON array
    qualifiers = db.Column(db.Text)     # JSON array
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_searched = db.Column(db.DateTime)

    articles = db.relationship('Article', backref='game', lazy='dynamic',
                              cascade='all, delete-orphan')

    @property
    def article_count(self):
        return self.articles.count()

class Article(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey('game.id'), nullable=False)
    title = db.Column(db.String(500), nullable=False)
    url = db.Column(db.String(1000), nullable=False)
    source_name = db.Column(db.String(200))
    published_at = db.Column(db.DateTime)
    description = db.Column(db.Text)
    image_url = db.Column(db.String(1000))
    sentiment_score = db.Column(db.Float)
    sentiment_label = db.Column(db.String(20))
    relevance_score = db.Column(db.Float)
    found_at = db.Column(db.DateTime, default=datetime.utcnow)
    reach = db.Column(db.BigInteger)        # estimated monthly unique visitors

    __table_args__ = (
        db.UniqueConstraint('game_id', 'url', name='unique_game_article'),
    )

with app.app_context():
    db.create_all()

# ==================== SOURCE REACH DATABASE ====================
SOURCE_REACH = {
    # Major gaming
    'ign': 92_000_000, 'gamespot': 40_000_000, 'pcgamer': 15_000_000,
    'eurogamer': 9_000_000, 'polygon': 14_000_000, 'kotaku': 7_500_000,
    'gamesradar': 13_000_000, 'rockpapershotgun': 5_900_000,
    'vg247': 2_200_000, 'destructoid': 5_300_000, 'nintendolife': 6_900_000,
    'pushsquare': 3_000_000, 'trueachievements': 5_000_000,
    'screenrant': 44_000_000, 'gamerant': 23_000_000,
    'dualshockers': 1_700_000, 'gematsu': 1_500_000,
    'rpgamer': 200_000, 'rpgsite': 1_900_000, 'gameinformer': 1_600_000,
    'toucharcade': 365_000, 'pocketgamer': 2_200_000,
    'siliconera': 1_000_000, 'rpgfan': 490_000, 'mmorpg': 930_000,
    'shacknews': 1_300_000, 'gamingbolt': 590_000, 'wccftech': 3_400_000,
    'pcgamesn': 4_000_000, 'gamedeveloper': 830_000,
    'gamedaily': 170_000, 'videogameschronicle': 4_000_000,
    'venturebeat': 2_400_000, 'gamewatcher': 440_000,
    'comicbook': 14_000_000, 'dexerto': 4_700_000,
    # General news with gaming sections
    'nme': 5_300_000, 'metro': 15_000_000, 'theguardian': 349_000_000,
    'nytimes': 676_000_000, 'forbes': 78_000_000,
    'washingtonpost': 89_000_000, 'variety': 29_000_000,
    'vice': 9_800_000, 'inverse': 2_300_000,
    'digitaltrends': 18_000_000, 'techradar': 18_000_000,
    'pcworld': 2_500_000, 'pcmag': 14_000_000,
    'telegraph': 63_000_000, 'independent': 62_000_000,
    'dailystar': 8_800_000, 'ladbible': 9_400_000,
    'sportingnews': 15_000_000, 'si': 39_000_000,
    'time': 11_000_000, 'radiotimes': 13_000_000,
    'digitalspy': 9_500_000, 'vgchartz': 680_000,
    'insider-gaming': 2_200_000, 'bleedingcool': 3_200_000,
    'escapist': 1_300_000,
    # European
    '3djuegos': 10_000_000, 'meristation': 1_300_000,
    'hobbyconsolas': 9_000_000, 'vandal': 16_000_000,
    'jeuxvideo': 29_000_000, 'gameblog': 2_900_000,
    'jeuxactu': 200_000, 'gamepro': 9_000_000,
    'gamestar': 15_000_000, 'giga': 15_000_000,
    '4players': 2_700_000, 'pcgames': 3_200_000,
    'eurogamer.de': 3_700_000, 'everyeye': 7_800_000,
    'multiplayer': 5_600_000, 'spaziogames': 1_000_000,
    'thegamesmachine': 170_000, 'gry-online': 8_100_000,
    'gram': 1_800_000, 'ppe': 5_200_000,
    'lowcygier': 3_000_000, 'gamer.nl': 10_000,
    'pu.nl': 210_000, 'xgn.nl': 450_000,
    'gamer.no': 800_000, 'gamereactor': 600_000,
    'fingerguns': 38_000, 'darkzero': 25_000,
    'thesixthaxis': 375_000, 'purexbox': 2_500_000,
    'pureplaystation': 1_500_000, 'xboxdynasty': 1_100_000,
    'xboxachievements': 580_000,
    # Asia/Pacific
    'inven': 51_000_000, 'sector': 1_600_000,
    'indian': 1_100_000, 'gamepressure': 2_300_000,
    'ixbt': 7_600_000, 'goha': 2_200_000,
    'rutab': 2_300_000, 'riotpixels': 3_000_000,
    'newxboxone': 730_000, 'stratege': 1_700_000,
    'gameshub': 590_000, 'stevivor': 130_000,
    'press-start': 330_000, 'wellplayed': 100_000,
    'checkpointgaming': 82_000, 'player2': 23_000,
    'vooks': 170_000, 'shindig': 3_000,
    'smh': 25_000_000,
    # Latin America
    'levelup': 860_000, 'atomix': 540_000,
    'tierragamer': 150_000, 'meups': 780_000,
    'psxbrasil': 640_000, 'tecmundo': 8_100_000,
    'adrenaline': 2_400_000, 'canaltech': 8_300_000,
    'flowgames': 300_000, 'gamersrd': 24_000,
    'psxextreme': 52_000, 'gamersegames': 19_000,
    'pizzafria': 44_000, 'gamefm': 13_000,
    'defesaperfeita': 7_000, 'dropsdejogos': 94_000,
    # Defaults
    'Google News': 100_000,
    'Unknown': 50_000,
}

def estimate_reach(source_name, url=''):
    if not source_name:
        return 50_000
    text = f"{source_name} {url}".lower()
    for key, reach in SOURCE_REACH.items():
        if key in text:
            return reach
    # fallback guesses
    major_news = ['nytimes', 'guardian', 'washingtonpost', 'forbes', 'wsj',
                  'bloomberg', 'reuters', 'bbc', 'cnn', 'telegraph', 'independent']
    for outlet in major_news:
        if outlet in text:
            return 50_000_000
    if any(ind in text for ind in ['game', 'gaming', 'xbox', 'playstation', 'nintendo',
                                   'steam', 'esports', 'rpg', 'mmo', 'indie game']):
        return 200_000
    if any(ind in text for ind in ['tech', 'digital', 'gadget', 'review', 'ai', 'software']):
        return 500_000
    return 100_000

# ==================== MULTI-REGION GOOGLE NEWS RSS ====================

REGIONS = {
    'NA': [('US','en'), ('CA','en'), ('CA','fr'), ('MX','es')],
    'EMEA': [
        ('GB','en'), ('DE','de'), ('FR','fr'), ('ES','es'), ('IT','it'),
        ('RU','ru'), ('AE','ar'), ('AE','en'), ('ZA','en'), ('NG','en'),
        ('SE','sv'), ('NO','no'), ('DK','da'), ('FI','fi'),
        ('NL','nl'), ('BE','nl'), ('BE','fr'), ('CH','de'), ('CH','fr'),
        ('AT','de'), ('PL','pl'), ('CZ','cs'), ('HU','hu'),
        ('RO','ro'), ('GR','el'), ('IL','he'), ('SA','ar')
    ],
    'EU': [
        ('GB','en'), ('DE','de'), ('FR','fr'), ('ES','es'), ('IT','it'),
        ('RU','ru'), ('SE','sv'), ('NO','no'), ('DK','da'), ('FI','fi'),
        ('NL','nl'), ('BE','nl'), ('BE','fr'), ('CH','de'), ('CH','fr'),
        ('AT','de'), ('PL','pl'), ('CZ','cs'), ('HU','hu'),
        ('RO','ro'), ('GR','el')
    ],
    'UK': [('GB','en')],
    'APJ': [
        ('JP','ja'), ('KR','ko'), ('CN','zh'), ('IN','en'), ('IN','hi'),
        ('AU','en'), ('NZ','en'), ('SG','en'), ('SG','zh'),
        ('MY','en'), ('MY','ms'), ('PH','en'), ('TH','th'),
        ('VN','vi'), ('ID','id')
    ],
    'SEA': [
        ('SG','en'), ('SG','zh'), ('MY','en'), ('MY','ms'),
        ('PH','en'), ('TH','th'), ('VN','vi'), ('ID','id')
    ],
    'China': [('CN','zh')],
}

def _fetch_single_rss(game_name, country_code, lang_code, when='7d', qualifiers=None):
    """Fetch Google News RSS for one country/language pair, with optional qualifiers."""
    if qualifiers:
        or_groups = []
        for q in qualifiers:
            q = q.strip()
            if '+' in q:
                and_parts = [p.strip() for p in q.split('+')]
                or_groups.append(' '.join(and_parts))
            else:
                or_groups.append(q)
        qualifier_str = ' OR '.join(f'"{g}"' for g in or_groups)
        search_query = f'"{game_name}" AND ({qualifier_str})'
    else:
        search_query = game_name

    query = urllib.parse.quote(search_query)
    ceid = f'{country_code}:{lang_code}' if lang_code else f'{country_code}'
    url = f"https://news.google.com/rss/search?q={query}&hl={lang_code}-{country_code}&gl={country_code}&ceid={ceid}&when={when}"
    articles = []
    try:
        resp = requests.get(url, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
        for entry in feed.entries:
            pub_date = None
            if hasattr(entry, 'published_parsed') and entry.published_parsed:
                pub_date = datetime(*entry.published_parsed[:6])
            elif hasattr(entry, 'updated_parsed') and entry.updated_parsed:
                pub_date = datetime(*entry.updated_parsed[:6])
            else:
                pub_date = datetime.now()
            # Real source name
            source_name = 'Unknown'
            if hasattr(entry, 'source') and entry.source:
                source_name = entry.source.get('title', 'Unknown')
            else:
                parts = entry.title.rsplit(' - ', 1)
                if len(parts) == 2:
                    source_name = parts[1].strip()
                else:
                    source_name = feed.feed.get('title', 'Google News')
            articles.append({
                'title': entry.title,
                'url': entry.link,
                'source_name': source_name,
                'published_at': pub_date,
                'description': entry.get('summary', '')[:1000],
                'image_url': ''
            })
    except Exception as e:
        logger.warning(f"Google News RSS error for {country_code}/{lang_code}: {e}")
    return articles

def search_google_news_rss(game_name, when='7d', qualifiers=None, max_pairs=None):
    """Search across all regions in parallel, deduplicate by URL."""
    pairs = set()
    for region_pairs in REGIONS.values():
        for pair in region_pairs:
            pairs.add(pair)
    
    # Limit the number of region/language pairs if specified
    if max_pairs and len(pairs) > max_pairs:
        import random
        pairs = set(random.sample(list(pairs), max_pairs))

    all_articles = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(_fetch_single_rss, game_name, cc, lc, when, qualifiers): (cc, lc)
            for (cc, lc) in pairs
        }
        for future in as_completed(futures):
            try:
                result = future.result()
                all_articles.extend(result)
            except Exception as e:
                logger.warning(f"Future error: {e}")

    seen_urls = set()
    unique_articles = []
    for art in all_articles:
        if art['url'] not in seen_urls:
            seen_urls.add(art['url'])
            unique_articles.append(art)

    logger.info(f"Google News RSS: {len(unique_articles)} unique articles for '{game_name}'")
    return unique_articles

def search_gnews_api(game_name, start_date=None, end_date=None, days_back=1):
    """Fallback: GNews API if key is available."""
    api_key = os.getenv('GNEWS_API_KEY', '')
    if not api_key:
        return []
    url = "https://gnews.io/api/v4/search"
    params = {
        'q': game_name,
        'lang': 'en',
        'max': 100,
        'apikey': api_key,
        'sort': 'relevance'
    }
    if start_date:
        params['from'] = start_date.strftime('%Y-%m-%dT%H:%M:%SZ')
    if end_date:
        params['to'] = end_date.strftime('%Y-%m-%dT%H:%M:%SZ')
    else:
        params['from'] = (datetime.now() - timedelta(days=days_back)).strftime('%Y-%m-%dT%H:%M:%SZ')
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        articles = []
        for item in data.get('articles', []):
            articles.append({
                'title': item['title'],
                'url': item['url'],
                'source_name': item['source']['name'],
                'published_at': datetime.strptime(item['publishedAt'][:19], '%Y-%m-%dT%H:%M:%S'),
                'description': item.get('description', ''),
                'image_url': item.get('image', '')
            })
        logger.info(f"GNews API returned {len(articles)} articles for '{game_name}'")
        return articles
    except Exception as e:
        logger.error(f"GNews API error for '{game_name}': {e}")
        return []

def fetch_all_articles(game, start_date=None, end_date=None):
    """Combine Google News RSS + GNews API, with date filtering and qualifier support."""
    if start_date and end_date:
        delta = max((end_date - start_date).days, 1)
        when = f'{delta}d'
    else:
        when = '7d'

    # Parse qualifiers for this game
    game_qualifiers = None
    if game.qualifiers:
        try:
            game_qualifiers = json.loads(game.qualifiers)
        except:
            pass

    articles = []
    articles.extend(search_google_news_rss(game.name, when=when, qualifiers=game_qualifiers))
    articles.extend(search_gnews_api(game.name, start_date=start_date, end_date=end_date))

    if start_date and end_date:
        filtered = []
        for art in articles:
            pub = art.get('published_at')
            if pub and start_date <= pub <= end_date:
                filtered.append(art)
        articles = filtered

    # Final dedup
    seen = set()
    unique = []
    for art in articles:
        if art['url'] not in seen:
            seen.add(art['url'])
            unique.append(art)
    return unique

# ==================== FALSE-POSITIVE FILTERING ====================

def is_gaming_article(title, description, game_name, qualifiers=None, strict_mode=False):
    text = f"{title} {description}".lower()
    game_lower = game_name.lower()

    default_gaming_terms = [
        'game', 'gaming', 'xbox', 'playstation', 'ps5', 'ps4', 'nintendo',
        'switch', 'steam', 'pc game', 'video game', 'dlc', 'update', 'patch',
        'developer', 'studio', 'release', 'launch', 'trailer', 'gameplay',
        'rpg', 'fps', 'indie', 'esports', 'review', 'score', 'deck'
    ]

    custom_terms = []
    if qualifiers:
        try:
            custom_terms = json.loads(qualifiers) if isinstance(qualifiers, str) else qualifiers
        except:
            pass

    if strict_mode and custom_terms:
        if game_lower not in text:
            return False
        or_groups = []
        current_group = []
        for term in custom_terms:
            term = term.strip()
            if '+' in term:
                and_terms = [t.strip() for t in term.split('+')]
                or_groups.append({'type': 'AND', 'terms': and_terms})
            else:
                current_group.append(term)
        if current_group:
            or_groups.append({'type': 'OR', 'terms': current_group})
        for group in or_groups:
            if group['type'] == 'AND':
                if all(t.lower() in text for t in group['terms']):
                    return True
            else:
                if any(t.lower() in text for t in group['terms']):
                    return True
        return False
    else:
        all_terms = default_gaming_terms.copy()
        if custom_terms:
            for t in custom_terms:
                all_terms.append(t.strip().lower())
        for term in all_terms:
            if term.lower() in text:
                return True
        return False

def is_gaming_source(source_name, url=''):
    gaming_domains = [
        'ign', 'gamespot', 'pcgamer', 'eurogamer', 'polygon', 'kotaku',
        'gamesradar', 'rockpapershotgun', 'vg247', 'destructoid', 'nintendolife',
        'pushsquare', 'trueachievements', 'screenrant', 'gamerant', 'dualshockers',
        'gematsu', 'rpgamer', 'rpgsite', 'gameinformer', 'toucharcade',
        'pocketgamer', 'siliconera', 'rpgfan', 'mmorpg', 'shacknews',
        'venturebeat/games', 'videogameschronicle', 'gamingbolt', 'wccftech',
        'gamewatcher', 'pcgamesn', 'gamedeveloper', 'gamedaily', 'gaming',
        'xbox', 'playstation', 'nintendo', 'steam deck'
    ]
    check_text = f"{source_name} {url}".lower()
    return any(domain in check_text for domain in gaming_domains)

# ==================== SENTIMENT ANALYSIS ====================

def analyze_sentiment(text):
    try:
        blob = TextBlob(text[:1000])
        score = blob.sentiment.polarity
        if score > 0.1:
            label = 'positive'
        elif score < -0.1:
            label = 'negative'
        else:
            label = 'neutral'
        return score, label
    except:
        return 0, 'neutral'

# ==================== SAVE ARTICLES ====================

def save_articles(game, articles_list):
    saved = 0
    skipped = 0
    qualifiers = None
    if game.qualifiers:
        try:
            qualifiers = json.loads(game.qualifiers)
        except:
            pass

    strict = bool(qualifiers)

    for art in articles_list:
        existing = Article.query.filter_by(game_id=game.id, url=art['url']).first()
        if existing:
            continue

        title = art.get('title', '')
        description = art.get('description', '')
        source_name = art.get('source_name', '')
        url = art.get('url', '')

        is_gaming = is_gaming_article(title, description, game.name, qualifiers, strict_mode=strict)
        is_gaming_src = is_gaming_source(source_name, url)
        if not is_gaming and not is_gaming_src:
            skipped += 1
            continue

        score, label = analyze_sentiment(f"{title} {description}")
        reach = estimate_reach(source_name, url)
        article = Article(
            game_id=game.id,
            title=title[:500],
            url=url[:1000],
            source_name=source_name[:200],
            published_at=art.get('published_at', datetime.now()),
            description=description[:1000] if description else '',
            image_url=art.get('image_url', '')[:1000],
            sentiment_score=score,
            sentiment_label=label,
            relevance_score=0.8,
            reach=reach
        )
        db.session.add(article)
        saved += 1

    db.session.commit()
    if skipped > 0:
        logger.info(f"Filtered out {skipped} non-gaming articles for '{game.name}'")
    return saved

# ==================== ROUTES ====================

@app.route('/')
# @require_auth       # uncomment to protect
def dashboard():
    games = Game.query.filter_by(active=True).all()
    total_articles = Article.query.count()
    week_ago = datetime.now() - timedelta(days=7)
    weekly_articles = Article.query.filter(Article.published_at >= week_ago).count()
    recent_articles = Article.query.order_by(Article.published_at.desc()).limit(20).all()
    game_stats = []
    for game in games:
        game_stats.append({
            'name': game.name,
            'total': game.article_count,
            'recent': game.articles.filter(Article.published_at >= week_ago).count()
        })
    return render_template('dashboard.html',
                         games=games,
                         total_articles=total_articles,
                         weekly_articles=weekly_articles,
                         recent_articles=recent_articles,
                         game_stats=game_stats)

@app.route('/games')
# @require_auth
def games_page():
    all_games = Game.query.order_by(Game.name).all()
    return render_template('games.html', games=all_games)

@app.route('/add-game', methods=['POST'])
# @require_auth
def add_game():
    name = request.form.get('name', '').strip()
    publisher = request.form.get('publisher', '').strip()
    platforms = request.form.get('platforms', '').strip()
    keywords_str = request.form.get('keywords', '').strip()
    qualifiers_str = request.form.get('qualifiers', '').strip()

    if not name:
        return redirect(url_for('games_page'))

    keywords = [k.strip() for k in keywords_str.split(',') if k.strip()] if keywords_str else []
    qualifiers = [q.strip() for q in qualifiers_str.split(',') if q.strip()] if qualifiers_str else []

    existing = Game.query.filter_by(name=name).first()
    if existing:
        return redirect(url_for('games_page'))

    game = Game(
        name=name,
        publisher=publisher,
        platforms=platforms,
        keywords=json.dumps(keywords),
        qualifiers=json.dumps(qualifiers)
    )
    db.session.add(game)
    db.session.commit()

    articles = fetch_all_articles(game)
    count = save_articles(game, articles)
    game.last_searched = datetime.utcnow()
    db.session.commit()
    logger.info(f"Added '{name}' – {count} initial articles")
    return redirect(url_for('games_page'))

@app.route('/toggle-game/<int:game_id>', methods=['POST'])
def toggle_game(game_id):
    game = Game.query.get_or_404(game_id)
    game.active = not game.active
    db.session.commit()
    return redirect(url_for('games_page'))

@app.route('/delete-game/<int:game_id>', methods=['POST'])
def delete_game(game_id):
    game = Game.query.get_or_404(game_id)
    db.session.delete(game)
    db.session.commit()
    return redirect(url_for('games_page'))

@app.route('/search/<int:game_id>')
def search_game_now(game_id):
    game = Game.query.get_or_404(game_id)
    start_str = request.args.get('start_date')
    end_str = request.args.get('end_date')
    start_date = None
    end_date = None
    if start_str:
        try:
            start_date = datetime.strptime(start_str, '%Y-%m-%d')
        except:
            pass
    if end_str:
        try:
            end_date = datetime.strptime(end_str, '%Y-%m-%d') + timedelta(days=1)
        except:
            pass
    articles = fetch_all_articles(game, start_date=start_date, end_date=end_date)
    count = save_articles(game, articles)
    game.last_searched = datetime.utcnow()
    db.session.commit()
    logger.info(f"Manual search for '{game.name}': found {count} new articles")
    return redirect(url_for('articles_page', game_id=game_id))

@app.route('/search-all')
def search_all_games():
    games = Game.query.filter_by(active=True).all()
    results = []
    for game in games:
        try:
            articles = fetch_all_articles(game)
            count = save_articles(game, articles)
            game.last_searched = datetime.utcnow()
            results.append({'game': game.name, 'articles_found': count, 'status': 'success'})
        except Exception as e:
            logger.error(f"Search failed for {game.name}: {e}")
            results.append({'game': game.name, 'articles_found': 0, 'status': 'error'})
    db.session.commit()
    return jsonify({'success': True, 'results': results})

@app.route('/articles')
# @require_auth
def articles_page():
    game_id = request.args.get('game_id', type=int)
    sentiment = request.args.get('sentiment', '')
    source = request.args.get('source', '')
    page = request.args.get('page', 1, type=int)
    query = Article.query
    if game_id:
        query = query.filter_by(game_id=game_id)
        selected_game = Game.query.get(game_id)
    else:
        selected_game = None
    if sentiment:
        query = query.filter_by(sentiment_label=sentiment)
    if source:
        query = query.filter(Article.source_name.contains(source))
    articles_paginated = query.order_by(Article.published_at.desc()).paginate(page=page, per_page=25)
    all_games = Game.query.all()
    sources = db.session.query(Article.source_name).distinct().order_by(Article.source_name).all()
    sources = [s[0] for s in sources if s[0]]
    return render_template('articles.html',
                         articles=articles_paginated,
                         games=all_games,
                         sources=sources,
                         selected_game=selected_game,
                         selected_sentiment=sentiment,
                         selected_source=source)

@app.route('/analytics/<int:game_id>')
# @require_auth
def analytics_page(game_id):
    game = Game.query.get_or_404(game_id)
    days = request.args.get('days', 30, type=int)
    since_date = datetime.now() - timedelta(days=days)
    articles = Article.query.filter(
        Article.game_id == game_id,
        Article.published_at >= since_date
    ).order_by(Article.published_at.desc()).all()
    sentiment_counts = {
        'positive': sum(1 for a in articles if a.sentiment_label == 'positive'),
        'negative': sum(1 for a in articles if a.sentiment_label == 'negative'),
        'neutral': sum(1 for a in articles if a.sentiment_label == 'neutral')
    }
    avg_sentiment = sum(a.sentiment_score or 0 for a in articles) / len(articles) if articles else 0
    daily_data = {}
    for a in articles:
        if a.published_at:
            key = a.published_at.strftime('%Y-%m-%d')
            daily_data.setdefault(key, {'total':0, 'positive':0, 'negative':0, 'neutral':0})
            daily_data[key]['total'] += 1
            daily_data[key][a.sentiment_label] += 1
    source_counts = {}
    for a in articles:
        source = a.source_name or 'Unknown'
        source_counts[source] = source_counts.get(source, 0) + 1
    top_sources = sorted(source_counts.items(), key=lambda x: x[1], reverse=True)[:10]
    top_articles = sorted(articles, key=lambda x: x.relevance_score or 0, reverse=True)[:10]
    return render_template('analytics.html',
                         game=game, articles=articles, days=days,
                         sentiment_counts=sentiment_counts,
                         avg_sentiment=round(avg_sentiment, 2),
                         daily_data=daily_data,
                         top_sources=top_sources,
                         top_articles=top_articles)

@app.route('/export/<int:game_id>')
# @require_auth
def export_csv(game_id):
    game = Game.query.get_or_404(game_id)
    try:
        articles = Article.query.filter_by(game_id=game_id).order_by(Article.published_at.desc()).all()
        si = StringIO()
        writer = csv.writer(si)
        writer.writerow(['Date', 'Title', 'Source', 'URL', 'Sentiment', 'Sentiment Score',
                         'Reach (Monthly)', 'EMV (USD)', 'Language', 'Region'])
        CPM = 35
        SENTIMENT_WEIGHT = {'positive': 1.2, 'neutral': 1.0, 'negative': 0.8}
        for a in articles:
            reach_val = a.reach or 50000
            base = reach_val / 1000 * CPM
            mult = SENTIMENT_WEIGHT.get(a.sentiment_label, 1.0)
            emv = round(base * mult, 2)
            # simplistic language/region detection
            lang = 'en'; region = 'Global'
            src_lower = (a.source_name or '').lower()
            if any(x in src_lower for x in ['de.', '.de', 'germany']):
                lang='de'; region='Germany'
            elif any(x in src_lower for x in ['fr.', '.fr', 'france']):
                lang='fr'; region='France'
            elif any(x in src_lower for x in ['es.', '.es', 'spain']):
                lang='es'; region='Spain'
            elif any(x in src_lower for x in ['it.', '.it', 'italy']):
                lang='it'; region='Italy'
            elif any(x in src_lower for x in ['pl.', '.pl', 'poland']):
                lang='pl'; region='Poland'
            elif any(x in src_lower for x in ['ru.', '.ru', 'russia']):
                lang='ru'; region='Russia'
            elif any(x in src_lower for x in ['br.', '.br', 'brazil']):
                lang='pt'; region='Brazil'
            elif any(x in src_lower for x in ['jp.', '.jp', 'japan']):
                lang='ja'; region='Japan'
            elif any(x in src_lower for x in ['kr.', '.kr', 'korea']):
                lang='ko'; region='South Korea'
            writer.writerow([
                a.published_at.strftime('%Y-%m-%d') if a.published_at else '',
                a.title,
                a.source_name or 'Unknown',
                a.url,
                a.sentiment_label or 'neutral',
                round(a.sentiment_score,2) if a.sentiment_score else 0,
                f"{reach_val:,}",
                f"${emv:,.2f}",
                lang, region
            ])
        output = BytesIO()
        output.write(si.getvalue().encode('utf-8-sig'))
        output.seek(0)
        return send_file(output, mimetype='text/csv', as_attachment=True,
                         download_name=f'{game.name}_PR_Full_Report.csv')
    except Exception as e:
        logger.error(f"Export error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/weekly-report')
# @require_auth
def weekly_report():
    try:
        week_ago = datetime.now() - timedelta(days=7)
        games = Game.query.filter_by(active=True).all()
        si = StringIO()
        writer = csv.writer(si)
        writer.writerow(['Game', 'Total Articles', 'Positive', 'Negative', 'Neutral', 'Avg Sentiment'])
        for game in games:
            articles = Article.query.filter(Article.game_id==game.id, Article.published_at>=week_ago).all()
            if articles:
                total = len(articles)
                pos = sum(1 for a in articles if a.sentiment_label=='positive')
                neg = sum(1 for a in articles if a.sentiment_label=='negative')
                neu = sum(1 for a in articles if a.sentiment_label=='neutral')
                avg = round(sum(a.sentiment_score or 0 for a in articles)/total, 2)
                writer.writerow([game.name, total, pos, neg, neu, avg])
        output = BytesIO()
        output.write(si.getvalue().encode('utf-8'))
        output.seek(0)
        return send_file(output, mimetype='text/csv', as_attachment=True,
                         download_name=f'Weekly_PR_Report_{datetime.now().strftime("%Y%m%d")}.csv')
    except Exception as e:
        logger.error(f"Weekly report error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/delete-article/<int:article_id>', methods=['POST'])
def delete_article(article_id):
    article = Article.query.get_or_404(article_id)
    game_id = article.game_id
    db.session.delete(article)
    db.session.commit()
    return redirect(url_for('articles_page', game_id=game_id))

@app.route('/ping')
def ping():
    return jsonify({'status': 'ok'})

@app.route('/daily-search')
def daily_search():
    games = Game.query.filter_by(active=True).all()
    total = 0
    for game in games:
        try:
            articles = fetch_all_articles(game)
            count = save_articles(game, articles)
            game.last_searched = datetime.utcnow()
            total += count
        except Exception as e:
            logger.error(f"Daily search failed for {game.name}: {e}")
    db.session.commit()
    return jsonify({'success': True, 'games_searched': len(games), 'new_articles': total})

@app.route('/fix-db')
def fix_database():
    from sqlalchemy import inspect, text
    inspector = inspect(db.engine)
    results = []
    # game columns
    game_cols = [c['name'] for c in inspector.get_columns('game')]
    if 'qualifiers' not in game_cols:
        db.session.execute(text('ALTER TABLE game ADD COLUMN qualifiers TEXT'))
        results.append('Added qualifiers column to game')
    # article columns
    art_cols = [c['name'] for c in inspector.get_columns('article')]
    if 'reach' not in art_cols:
        db.session.execute(text('ALTER TABLE article ADD COLUMN reach BIGINT'))
        results.append('Added reach column to article')
    db.session.commit()
    if not results:
        results.append('All columns already present')
    return jsonify({'status': 'success', 'messages': results})

# ==================== SCHEDULER ====================

def init_scheduler():
    scheduler = BackgroundScheduler()
    def scheduled_daily():
        with app.app_context():
            games = Game.query.filter_by(active=True).all()
            for game in games:
                try:
                    articles = fetch_all_articles(game)
                    save_articles(game, articles)
                    game.last_searched = datetime.utcnow()
                except Exception as e:
                    logger.error(f"Scheduled search failed for {game.name}: {e}")
            db.session.commit()
    scheduler.add_job(scheduled_daily, 'cron', hour=6, minute=0)
    scheduler.start()
    atexit.register(lambda: scheduler.shutdown())
    return scheduler

with app.app_context():
    scheduler = init_scheduler()

if __name__ == '__main__':
    app.run(debug=True)
