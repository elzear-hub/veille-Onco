"""
Veille scientifique automatique en cancérologie
Journaux : NEJM, Lancet Oncology, Nature Medicine
Résumés en français, 3 articles les plus pertinents
"""

import requests
import smtplib
import json
import os
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ─── CONFIGURATION (à remplir une seule fois) ────────────────────────────────
GROQ_API_KEY   = os.environ["GROQ_API_KEY"]    # clé Groq
GMAIL_ADDRESS  = os.environ["GMAIL_ADDRESS"]   # ton adresse Gmail
GMAIL_PASSWORD = os.environ["GMAIL_PASSWORD"]  # mot de passe d'application Gmail
EMAIL_DEST     = os.environ["EMAIL_DEST"]      # où envoyer le digest (peut être le même Gmail)

# Journaux ciblés (noms exacts PubMed)
JOURNALS = [
    "N Engl J Med",
    "Lancet Oncol",
    "Nat Med",
]

# Mots-clés oncologie pour filtrer les articles vraiment pertinents
KEYWORDS = "cancer OR oncology OR tumor OR carcinoma OR chemotherapy OR immunotherapy OR radiotherapy"

MAX_ARTICLES = 3
# ─────────────────────────────────────────────────────────────────────────────


def fetch_pubmed_articles():
    """Récupère les articles des 2 derniers jours sur PubMed."""
    since = (datetime.now() - timedelta(days=2)).strftime("%Y/%m/%d")
    journal_filter = " OR ".join([f'"{j}"[Journal]' for j in JOURNALS])
    query = f"({KEYWORDS}) AND ({journal_filter}) AND (\"{since}\"[PDAT] : \"3000\"[PDAT])"

    # Recherche des IDs
    search_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
    params = {
        "db": "pubmed",
        "term": query,
        "retmax": 20,
        "retmode": "json",
        "sort": "relevance",
    }
    r = requests.get(search_url, params=params, timeout=15)
    r.raise_for_status()
    ids = r.json().get("esearchresult", {}).get("idlist", [])

    if not ids:
        return []

    # Récupération des détails
    fetch_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
    fetch_params = {
        "db": "pubmed",
        "id": ",".join(ids),
        "retmode": "xml",
        "rettype": "abstract",
    }
    rf = requests.get(fetch_url, params=fetch_params, timeout=15)
    rf.raise_for_status()

    # Parse XML simplifié
    import xml.etree.ElementTree as ET
    root = ET.fromstring(rf.text)
    articles = []

    for article in root.findall(".//PubmedArticle"):
        try:
            title = article.findtext(".//ArticleTitle", "").strip()
            abstract_parts = article.findall(".//AbstractText")
            abstract = " ".join(p.text or "" for p in abstract_parts if p.text).strip()
            journal = article.findtext(".//Journal/ISOAbbreviation", "").strip()
            pmid = article.findtext(".//PMID", "").strip()
            authors = article.findall(".//Author")
            first_author = ""
            if authors:
                last = authors[0].findtext("LastName", "")
                first = authors[0].findtext("ForeName", "")
                first_author = f"{last} {first}".strip()

            if title and abstract and len(abstract) > 100:
                articles.append({
                    "title": title,
                    "abstract": abstract[:2000],
                    "journal": journal,
                    "pmid": pmid,
                    "author": first_author,
                    "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                })
        except Exception:
            continue

    return articles


def score_relevance(articles):
    """Sélectionne les 3 articles les plus pertinents via Groq."""
    if not articles:
        return []

    if len(articles) <= MAX_ARTICLES:
        return articles

    # Demande à Groq de classer par pertinence clinique
    titles_list = "\n".join(
        f"{i+1}. {a['title']} ({a['journal']})" for i, a in enumerate(articles)
    )
    prompt = f"""Tu es un oncologue expert. Voici une liste d'articles scientifiques récents.
Sélectionne les {MAX_ARTICLES} articles les plus importants pour la pratique clinique en oncologie.
Réponds UNIQUEMENT avec les numéros séparés par des virgules, exemple: 2,5,1

Articles:
{titles_list}"""

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 20,
        "temperature": 0.1,
    }
    r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                      headers=headers, json=payload, timeout=30)
    r.raise_for_status()
    response = r.json()["choices"][0]["message"]["content"].strip()

    try:
        indices = [int(x.strip()) - 1 for x in response.split(",") if x.strip().isdigit()]
        selected = [articles[i] for i in indices if 0 <= i < len(articles)]
        return selected[:MAX_ARTICLES]
    except Exception:
        return articles[:MAX_ARTICLES]


def summarize_article(article):
    """Résume un article en français via Groq."""
    prompt = f"""Tu es un médecin oncologue. Résume cet article scientifique en français pour un confrère.

Titre : {article['title']}
Journal : {article['journal']}
Résumé original : {article['abstract']}

Fournis un résumé structuré en français avec exactement ce format :
🎯 OBJECTIF : (1 phrase)
🔬 MÉTHODE : (1-2 phrases)
📊 RÉSULTATS CLÉS : (2-3 points essentiels)
💡 IMPLICATION CLINIQUE : (1 phrase sur l'impact pratique)"""

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 400,
        "temperature": 0.3,
    }
    r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                      headers=headers, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def build_email_html(articles_with_summaries):
    """Construit le corps HTML de l'email."""
    date_str = datetime.now().strftime("%d %B %Y")
    count = len(articles_with_summaries)

    cards = ""
    for i, (article, summary) in enumerate(articles_with_summaries, 1):
        summary_html = summary.replace("\n", "<br>")
        cards += f"""
        <div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:12px;
                    padding:24px;margin-bottom:20px;">
          <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px;">
            <span style="background:#EEF2FF;color:#4338CA;font-size:12px;font-weight:600;
                         padding:3px 10px;border-radius:20px;">{article['journal']}</span>
            <span style="color:#9ca3af;font-size:12px;">{article['author']}</span>
          </div>
          <h3 style="margin:0 0 14px;font-size:15px;color:#111827;line-height:1.5;">
            {article['title']}
          </h3>
          <div style="font-size:14px;color:#374151;line-height:1.8;">
            {summary_html}
          </div>
          <a href="{article['url']}"
             style="display:inline-block;margin-top:14px;font-size:13px;
                    color:#4F46E5;text-decoration:none;">
            → Lire l'article complet sur PubMed
          </a>
        </div>"""

    if not articles_with_summaries:
        cards = """<div style="text-align:center;padding:40px;color:#6b7280;">
            Aucun nouvel article trouvé aujourd'hui dans les journaux sélectionnés.
        </div>"""

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:-apple-system,BlinkMacSystemFont,
             'Segoe UI',sans-serif;">
  <div style="max-width:640px;margin:0 auto;padding:32px 16px;">

    <div style="text-align:center;margin-bottom:28px;">
      <h1 style="font-size:22px;color:#111827;margin:0 0 6px;">
        Veille Oncologie
      </h1>
      <p style="color:#6b7280;font-size:14px;margin:0;">
        {date_str} · {count} article(s) sélectionné(s) · NEJM, Lancet Oncology, Nature Medicine
      </p>
    </div>

    {cards}

    <div style="text-align:center;margin-top:28px;color:#9ca3af;font-size:12px;">
      Généré automatiquement · Résumés par IA · Toujours vérifier l'article original
    </div>
  </div>
</body>
</html>"""
    return html


def send_email(html_content, article_count):
    """Envoie l'email via Gmail SMTP."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🔬 Veille Oncologie — {article_count} article(s) — {datetime.now().strftime('%d/%m/%Y')}"
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = EMAIL_DEST
    msg.attach(MIMEText(html_content, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, EMAIL_DEST, msg.as_string())


def main():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Démarrage de la veille oncologie...")

    print("  → Recherche des articles sur PubMed...")
    articles = fetch_pubmed_articles()
    print(f"  → {len(articles)} article(s) trouvé(s)")

    if articles:
        print("  → Sélection des plus pertinents...")
        selected = score_relevance(articles)
        print(f"  → {len(selected)} article(s) sélectionné(s)")

        print("  → Génération des résumés en français...")
        summaries = []
        for i, article in enumerate(selected, 1):
            print(f"     Résumé {i}/{len(selected)} : {article['title'][:60]}...")
            summary = summarize_article(article)
            summaries.append((article, summary))
    else:
        summaries = []

    print("  → Construction et envoi de l'email...")
    html = build_email_html(summaries)
    send_email(html, len(summaries))
    print("  ✓ Email envoyé avec succès !")


if __name__ == "__main__":
    main()
