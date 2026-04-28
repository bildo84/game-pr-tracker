from flask import Flask, render_template, request, jsonify, send_file, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta
import requests
import feedparser
from textblob import TextBlob
import json
import os
import pandas as pd
from io import BytesIO
from apscheduler.schedulers.background import BackgroundScheduler
import logging
import atexit

# Setup
app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-key-please-change')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///pr_tracker.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==================== MODELS ====================

class Game(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False, unique=True)
    publisher = db.Column(db.String(200))
    platforms = db.Column(db.String(200))
    keywords = db.Column(db.Text)  # JSON array
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_searched = db.Column(db.DateTime)
    
    articles = db.relationship('Article', backref='game', lazy='dynamic', cascade='all, delete-orphan')
    
    @property
    def article_count(self):
        return self.articles.count()
    
    @property
    def recent_articles(self):
        week_ago = datetime.now() - timedelta(days=7)
        return self.articles.filter(Article.published_at >= week_ago).count()

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
    
    __table_args__ = (
        db.UniqueConstraint('game_id', 'url', name='unique_game_article'),
    )

# Create tables
with app.app_context():
    db.create_all()

# ==================== SEARCH FUNCTIONS ====================

def search_gnews(game_name, days_back=1):
    """Search GNews API"""
    api_key = os.getenv('GNEWS_API_KEY', '')
    if not api_key:
        logger.warning("No GNEWS_API_KEY set")
        return []
    
    try:
        url = "https://gnews.io/api/v4/search"
        params = {
            'q': f'"{game_name}" video game',
            'lang': 'en',
            'max': 10,
            'from': (datetime.now() - timedelta(days=days_back)).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'apikey': api_key
        }
        
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        
        articles = []
        for item in response.json().get('articles', []):
            articles.append({
                'title': item['title'],
                'url': item['url'],
                'source_name': item['source']['name'],
                'published_at': datetime.strptime(item['publishedAt'][:19], '%Y-%m-%dT%H:%M:%S'),
                'description': item.get('description', ''),
                'image_url': item.get('image', '')
            })
        
        return articles
    except Exception as e:
        logger.error(f"GNews error for '{game_name}': {e}")
        return []

def search_rss(game_name):
    """Search gaming RSS feeds"""
    feeds = [
        ('PC Gamer', 'https://www.pcgamer.com/rss/'),
        ('GameSpot', 'https://www.gamespot.com/feeds/news/'),
        ('IGN', 'https://www.ign.com/rss/articles'),
        ('Kotaku', 'https://kotaku.com/rss'),
        ('Eurogamer', 'https://www.eurogamer.net/feed'),
        ('Polygon', 'https://www.polygon.com/rss/index.xml'),
    ]
    
    articles = []
    for source_name, feed_url in feeds:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:5]:
                title_lower = entry.title.lower()
                if game_name.lower() in title_lower:
                    pub_date = None
                    if hasattr(entry, 'published_parsed') and entry.published_parsed:
                        pub_date = datetime(*entry.published_parsed[:6])
                    elif hasattr(entry, 'updated_parsed') and entry.updated_parsed:
                        pub_date = datetime(*entry.updated_parsed[:6])
                    else:
                        pub_date = datetime.now()
                    
                    articles.append({
                        'title': entry.title,
                        'url': entry.link,
                        'source_name': source_name,
                        'published_at': pub_date,
                        'description': entry.get('summary', '')[:1000],
                        'image_url': ''
                    })
        except Exception as e:
            logger.error(f"RSS error for {source_name}: {e}")
    
    return articles

def analyze_sentiment(text):
    """Analyze sentiment of text"""
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

def calculate_relevance(title, description, game_name):
    """Simple relevance scoring"""
    text = f"{title} {description}".lower()
    game_lower = game_name.lower()
    
    score = 0.3  # Base score
    
    # Title contains game name
    if game_lower in title.lower():
        score += 0.4
    
    # Multiple mentions
    count = text.count(game_lower)
    score += min(count * 0.1, 0.3)
    
    return min(score, 1.0)

def search_and_save_articles(game):
    """Search for articles and save them to database"""
    all_articles = []
    all_articles.extend(search_gnews(game.name))
    all_articles.extend(search_rss(game.name))
    
    # Also search with keywords
    try:
        keywords = json.loads(game.keywords) if game.keywords else []
        for keyword in keywords[:3]:
            all_articles.extend(search_gnews(f"{game.name} {keyword}"))
    except:
        pass
    
    saved_count = 0
    for article_data in all_articles:
        # Check for duplicate
        existing = Article.query.filter_by(
            game_id=game.id,
            url=article_data['url']
        ).first()
        
        if not existing:
            # Analyze sentiment
            text = f"{article_data.get('title', '')} {article_data.get('description', '')}"
            sentiment_score, sentiment_label = analyze_sentiment(text)
            
            # Calculate relevance
            relevance = calculate_relevance(
                article_data.get('title', ''),
                article_data.get('description', ''),
                game.name
            )
            
            article = Article(
                game_id=game.id,
                title=article_data['title'][:500],
                url=article_data['url'][:1000],
                source_name=article_data.get('source_name', 'Unknown')[:200],
                published_at=article_data.get('published_at', datetime.now()),
                description=article_data.get('description', ''),
                image_url=article_data.get('image_url', '')[:1000],
                sentiment_score=sentiment_score,
                sentiment_label=sentiment_label,
                relevance_score=relevance
            )
            
            db.session.add(article)
            saved_count += 1
    
    game.last_searched = datetime.utcnow()
    db.session.commit()
    
    return saved_count

# ==================== ROUTES ====================

@app.route('/')
def dashboard():
    """Main dashboard"""
    games = Game.query.filter_by(active=True).all()
    total_articles = Article.query.count()
    
    week_ago = datetime.now() - timedelta(days=7)
    weekly_articles = Article.query.filter(Article.published_at >= week_ago).count()
    
    recent_articles = Article.query.order_by(
        Article.published_at.desc()
    ).limit(20).all()
    
    # Top games by coverage
    game_stats = []
    for game in games:
        game_stats.append({
            'name': game.name,
            'total': game.article_count,
            'recent': game.recent_articles
        })
    
    return render_template('dashboard.html',
                         games=games,
                         total_articles=total_articles,
                         weekly_articles=weekly_articles,
                         recent_articles=recent_articles,
                         game_stats=game_stats)

@app.route('/games')
def games_page():
    """Game management page"""
    all_games = Game.query.order_by(Game.name).all()
    return render_template('games.html', games=all_games)

@app.route('/add-game', methods=['POST'])
def add_game():
    """Add a new game"""
    name = request.form.get('name', '').strip()
    publisher = request.form.get('publisher', '').strip()
    platforms = request.form.get('platforms', '').strip()
    keywords_str = request.form.get('keywords', '').strip()
    
    if not name:
        return redirect(url_for('games_page'))
    
    # Parse keywords
    keywords = []
    if keywords_str:
        keywords = [k.strip() for k in keywords_str.split(',') if k.strip()]
    
    # Check for duplicates
    existing = Game.query.filter_by(name=name).first()
    if existing:
        return redirect(url_for('games_page'))
    
    game = Game(
        name=name,
        publisher=publisher,
        platforms=platforms,
        keywords=json.dumps(keywords)
    )
    
    db.session.add(game)
    db.session.commit()
    
    # Immediately search for this game
    count = search_and_save_articles(game)
    logger.info(f"Added game '{name}' with {count} initial articles")
    
    return redirect(url_for('games_page'))

@app.route('/toggle-game/<int:game_id>', methods=['POST'])
def toggle_game(game_id):
    """Toggle game active status"""
    game = Game.query.get_or_404(game_id)
    game.active = not game.active
    db.session.commit()
    
    status = "activated" if game.active else "deactivated"
    logger.info(f"Game '{game.name}' {status}")
    
    return redirect(url_for('games_page'))

@app.route('/delete-game/<int:game_id>', methods=['POST'])
def delete_game(game_id):
    """Delete a game and all its articles"""
    game = Game.query.get_or_404(game_id)
    game_name = game.name
    
    db.session.delete(game)
    db.session.commit()
    
    logger.info(f"Deleted game '{game_name}'")
    return redirect(url_for('games_page'))

@app.route('/search/<int:game_id>')
def search_game_now(game_id):
    """Search for articles about a specific game"""
    game = Game.query.get_or_404(game_id)
    count = search_and_save_articles(game)
    
    logger.info(f"Manual search for '{game.name}': found {count} new articles")
    
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'success': True, 'articles_found': count})
    
    return redirect(url_for('articles_page', game_id=game_id))

@app.route('/search-all')
def search_all_games():
    """Search for all active games"""
    games = Game.query.filter_by(active=True).all()
    results = []
    
    for game in games:
        try:
            count = search_and_save_articles(game)
            results.append({
                'game': game.name,
                'articles_found': count,
                'status': 'success'
            })
        except Exception as e:
            logger.error(f"Search failed for {game.name}: {e}")
            results.append({
                'game': game.name,
                'articles_found': 0,
                'status': 'error'
            })
    
    return jsonify({'success': True, 'results': results})

@app.route('/articles')
def articles_page():
    """View articles with filters"""
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
    
    articles_paginated = query.order_by(
        Article.published_at.desc()
    ).paginate(page=page, per_page=25)
    
    all_games = Game.query.all()
    
    # Get unique sources for filter
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
def analytics_page(game_id):
    """View analytics for a game"""
    game = Game.query.get_or_404(game_id)
    days = request.args.get('days', 30, type=int)
    
    since_date = datetime.now() - timedelta(days=days)
    articles = Article.query.filter(
        Article.game_id == game_id,
        Article.published_at >= since_date
    ).order_by(Article.published_at.desc()).all()
    
    # Sentiment breakdown
    sentiment_counts = {
        'positive': sum(1 for a in articles if a.sentiment_label == 'positive'),
        'negative': sum(1 for a in articles if a.sentiment_label == 'negative'),
        'neutral': sum(1 for a in articles if a.sentiment_label == 'neutral')
    }
    
    avg_sentiment = sum(a.sentiment_score for a in articles) / len(articles) if articles else 0
    
    # Daily breakdown for chart
    daily_data = {}
    for article in articles:
        if article.published_at:
            date_key = article.published_at.strftime('%Y-%m-%d')
            if date_key not in daily_data:
                daily_data[date_key] = {'total': 0, 'positive': 0, 'negative': 0, 'neutral': 0}
            daily_data[date_key]['total'] += 1
            daily_data[date_key][article.sentiment_label] += 1
    
    # Source breakdown
    source_counts = {}
    for article in articles:
        source = article.source_name or 'Unknown'
        source_counts[source] = source_counts.get(source, 0) + 1
    
    top_sources = sorted(source_counts.items(), key=lambda x: x[1], reverse=True)[:10]
    
    # Top articles by relevance
    top_articles = sorted(articles, key=lambda x: x.relevance_score or 0, reverse=True)[:10]
    
    return render_template('analytics.html',
                         game=game,
                         articles=articles,
                         days=days,
                         sentiment_counts=sentiment_counts,
                         avg_sentiment=round(avg_sentiment, 2),
                         daily_data=daily_data,
                         top_sources=top_sources,
                         top_articles=top_articles)

@app.route('/export/<int:game_id>')
def export_csv(game_id):
    """Export articles to Excel"""
    game = Game.query.get_or_404(game_id)
    
    articles = Article.query.filter_by(game_id=game_id).order_by(
        Article.published_at.desc()
    ).all()
    
    data = []
    for article in articles:
        data.append({
            'Date': article.published_at.strftime('%Y-%m-%d') if article.published_at else '',
            'Title': article.title,
            'Source': article.source_name,
            'URL': article.url,
            'Sentiment': article.sentiment_label,
            'Sentiment Score': round(article.sentiment_score, 2) if article.sentiment_score else 0,
            'Relevance Score': round(article.relevance_score, 2) if article.relevance_score else 0
        })
    
    df = pd.DataFrame(data)
    
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, sheet_name='Articles', index=False)
    output.seek(0)
    
    return send_file(
        output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name=f'{game.name}_PR_Report.xlsx'
    )

@app.route('/api/weekly-report')
def weekly_report():
    """Generate weekly PR report for all games"""
    week_ago = datetime.now() - timedelta(days=7)
    
    games = Game.query.filter_by(active=True).all()
    
    report_data = []
    for game in games:
        articles = Article.query.filter(
            Article.game_id == game.id,
            Article.published_at >= week_ago
        ).all()
        
        if articles:
            report_data.append({
                'Game': game.name,
                'Total Articles': len(articles),
                'Positive': sum(1 for a in articles if a.sentiment_label == 'positive'),
                'Negative': sum(1 for a in articles if a.sentiment_label == 'negative'),
                'Neutral': sum(1 for a in articles if a.sentiment_label == 'neutral'),
                'Avg Sentiment': round(sum(a.sentiment_score for a in articles) / len(articles), 2),
                'Top Sources': ', '.join(set(a.source_name for a in articles)[:3])
            })
    
    df = pd.DataFrame(report_data)
    
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, sheet_name='Weekly Report', index=False)
    output.seek(0)
    
    return send_file(
        output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name=f'Weekly_PR_Report_{datetime.now().strftime("%Y%m%d")}.xlsx'
    )

# Health check endpoint for cron-job.org
@app.route('/ping')
def ping():
    return jsonify({'status': 'ok', 'timestamp': datetime.utcnow().isoformat()})

# Daily search trigger (called by cron-job.org)
@app.route('/daily-search')
def daily_search():
    """Trigger daily search for all active games"""
    games = Game.query.filter_by(active=True).all()
    total_articles = 0
    
    for game in games:
        try:
            count = search_and_save_articles(game)
            total_articles += count
            logger.info(f"Daily search: {count} new articles for {game.name}")
        except Exception as e:
            logger.error(f"Daily search failed for {game.name}: {e}")
    
    return jsonify({
        'success': True,
        'games_searched': len(games),
        'new_articles': total_articles,
        'timestamp': datetime.utcnow().isoformat()
    })

# ==================== SCHEDULER (BACKUP) ====================

def init_scheduler():
    """Initialize APScheduler as backup (primary will be cron-job.org)"""
    scheduler = BackgroundScheduler()
    
    def scheduled_daily_search():
        with app.app_context():
            logger.info("Running scheduled daily search")
            games = Game.query.filter_by(active=True).all()
            for game in games:
                try:
                    search_and_save_articles(game)
                except Exception as e:
                    logger.error(f"Scheduled search failed for {game.name}: {e}")
    
    # Run at 6 AM daily as backup
    scheduler.add_job(scheduled_daily_search, 'cron', hour=6, minute=0)
    scheduler.start()
    
    # Shut down scheduler when app stops
    atexit.register(lambda: scheduler.shutdown())
    
    return scheduler

# Initialize scheduler
with app.app_context():
    scheduler = init_scheduler()
    logger.info("Application started with scheduler")

if __name__ == '__main__':
    app.run(debug=True)