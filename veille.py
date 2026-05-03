"""
Veille scientifique automatique en cancérologie v3
- 1 article par journal par jour (rotation progressive)
- Mémoire des articles déjà envoyés (sent_articles.json)
- Liste de subscribers (subscribers.json)
- Ton pédagogique rigoureux pour étudiants M2
- Journaux : NEJM, Lancet Oncology, Nature Medicine
"""

import requests
import smtplib
import json
import os
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import xml.etree.ElementTree as ET
import base64

# ─── CONFIGURATION ────────────────────────────────────────────────────────────
GROQ_API_KEY   = os.environ["GROQ_API_KEY"]
GMAIL_ADDRESS  = os.environ["GMAIL_ADDRESS"]
GMAIL_PASSWORD = os.environ["GMAIL_PASSWORD"]
GITHUB_TOKEN   = os.environ["GITHUB_TOKEN"]
GITHUB_REPO    = os.environ["GITHUB_REPO"]

JOURNALS = [
    "N Engl J Med",
    "Lancet Oncol",
    "Nat Med",
]

KEYWORDS = "cancer OR oncology OR tumor OR carcinoma OR chemotherapy OR immunotherapy OR radiotherapy"
DAYS_BACK = 30
MEMORY_FILE = "sent_articles.json"
SUBSCRIBERS_FILE = "subscribers.json"
# ─────────────────────────────────────────────────────────────────────────────


# ── GitHub helpers ────────────────────────────────────────────────────────────

def github_get(filename):
    """Lit un fichier JSON depuis GitHub, retourne (data, sha)."""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}"}
    r = requests.get(url, headers=headers, timeout=15)
    if r.status_code == 404:
        return None, None
    r.raise_for_status()
    content = base64.b64decode(r.json()["content"]).decode("utf-8")
    return json.loads(content), r.json()["sha"]


def github_put(filename, data, sha, message):
    """Écrit un fichier JSON dans GitHub."""
    content = base64.b64encode(json.dumps(data, indent=2).encode()).decode()
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {"message": message, "content": content}
    if sha:
        payload["sha"] = sha
    r = requests.put(url, headers=headers, json=payload, timeout=15)
    r.raise_for_status()


def load_subscribers():
    """Charge la liste des emails depuis subscribers.json."""
    data, _ = github_get(SUBSCRIBERS_FILE)
    if not data:
        return []
    return data.get("emails", [])


def load_memory():
    data, sha = github_get(MEMORY_FILE)
    if not data:
        return {"sent": [], "_sha": None}
    data["_sha"] = sha
    return data


def save_memory(memory):
    sha = memory.pop("_sha", None)
    github_put(
        MEMORY_FILE,
        memory,
        sha,
        f"Mise à jour mémoire {datetime.now().strftime('%Y-%m-%d')}"
    )


# ── PubMed ────────────────────────────────────────────────────────────────────

def fetch_articles_for_journal(journal):
    """Récupère les articles des 30 derniers jours pour un journal."""
    since = (datetime.now() - timedelta(days=DAYS_BACK)).strftime("%Y/%m/%d")
    query = f'({KEYWORDS}) AND "{journal}"[Journal] AND ("{since}"[PDAT] : "3000"[PDAT])'

    search_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
    params = {
        "db": "pubmed", "term": query,
        "retmax": 50, "retmode": "json", "sort": "pub+date",
    }
    r = requests.get(search_url, params=params, timeout=15)
    r.raise_for_status()
    ids = r.json().get("esearchresult", {}).get("idlist", [])
    if not ids:
        return []

    rf = requests.get(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
        params={"db": "pubmed", "id": ",".join(ids), "retmode": "xml", "rettype": "abstract"},
        timeout=15
    )
    rf.raise_for_status()

    root = ET.fromstring(rf.text)
    articles = []
    for article in root.findall(".//PubmedArticle"):
        try:
            title = article.findtext(".//ArticleTitle", "").strip()
            abstract_parts = article.findall(".//AbstractText")
            abstract = " ".join(p.text or "" for p in abstract_parts if p.text).strip()
            pmid = article.findtext(".//PMID", "").strip()
            authors = article.findall(".//Author")
            first_author = ""
            if authors:
                last = authors[0].findtext("LastName", "")
                fore = authors[0].findtext("ForeName", "")
                first_author = f"{last} {fore}".strip()
            pub_year = article.findtext(".//PubDate/Year", "")

            if title and abstract and len(abstract) > 100:
                articles.append({
                    "pmid": pmid, "title": title,
                    "abstract": abstract[:2000], "journal": journal,
                    "author": first_author, "year": pub_year,
                    "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                })
        except Exception:
            continue
    return articles


def pick_next_article(articles, sent_pmids):
    for article in articles:
        if article["pmid"] not in sent_pmids:
            return article
    return articles[0] if articles else None


# ── Groq ──────────────────────────────────────────────────────────────────────

def summarize_article(article):
    prompt = f"""Tu es un enseignant-chercheur en oncologie. Rédige un résumé pédagogique de cet article pour des étudiants en Master 2 de cancérologie.

Le résumé doit être rigoureux scientifiquement, utiliser la terminologie appropriée, mais rester accessible et formatif. Explique brièvement le contexte si nécessaire.

Titre : {article['title']}
Journal : {article['journal']}
Résumé original : {article['abstract']}

Format attendu en français :
🔬 CONTEXTE & OBJECTIF : (2-3 phrases : pourquoi cette question est importante, quel est l'objectif)
⚗️ MÉTHODE : (design de l'étude, population, intervention, critères de jugement principaux)
📊 RÉSULTATS : (résultats clés avec données chiffrées si disponibles)
🧠 INTERPRÉTATION : (ce que ça signifie, limites éventuelles, niveau de preuve)
💡 À RETENIR : (1 phrase synthétique pour un étudiant en M2)"""

    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 500, "temperature": 0.3,
    }
    r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                      headers=headers, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


# ── Email ─────────────────────────────────────────────────────────────────────

def build_email_html(articles_with_summaries):
    date_str = datetime.now().strftime("%d %B %Y")
    cards = ""
    for article, summary in articles_with_summaries:
        summary_html = summary.replace("\n", "<br>")
        cards += f"""
        <div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:12px;
                    padding:24px;margin-bottom:20px;">
          <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px;flex-wrap:wrap;">
            <span style="background:#EEF2FF;color:#4338CA;font-size:12px;font-weight:600;
                         padding:3px 10px;border-radius:20px;">{article['journal']}</span>
            <span style="color:#9ca3af;font-size:12px;">{article['author']} {article['year']}</span>
          </div>
          <h3 style="margin:0 0 14px;font-size:15px;color:#111827;line-height:1.5;">
            {article['title']}
          </h3>
          <div style="font-size:14px;color:#374151;line-height:1.9;">
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
            Aucun nouvel article disponible aujourd'hui.
        </div>"""

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  <div style="max-width:640px;margin:0 auto;padding:32px 16px;">
    <div style="text-align:center;margin-bottom:28px;">
      <h1 style="font-size:22px;color:#111827;margin:0 0 6px;">Veille Oncologie</h1>
      <p style="color:#6b7280;font-size:14px;margin:0;">
        {date_str} · 1 article par journal · NEJM, Lancet Oncology, Nature Medicine
      </p>
    </div>
    {cards}
    <div style="text-align:center;margin-top:28px;color:#9ca3af;font-size:12px;">
      Veille automatique M2 Oncologie · Résumés par IA · Toujours vérifier l'article original
    </div>
  </div>
</body></html>"""


def send_email(html_content, recipients):
    """Envoie l'email à tous les subscribers."""
    if not recipients:
        print("  ⚠ Aucun subscriber trouvé !")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🔬 Veille Oncologie M2 — {datetime.now().strftime('%d/%m/%Y')}"
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html_content, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, recipients, msg.as_string())

    print(f"  ✓ Email envoyé à {len(recipients)} destinataire(s)")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Démarrage veille oncologie v3...")

    print("  → Chargement des subscribers...")
    recipients = load_subscribers()
    print(f"  → {len(recipients)} destinataire(s)")

    print("  → Chargement de la mémoire...")
    memory = load_memory()
    sent_pmids = set(memory.get("sent", []))

    results = []
    new_sent = []

    for journal in JOURNALS:
        print(f"  → Recherche : {journal}")
        articles = fetch_articles_for_journal(journal)
        print(f"     {len(articles)} article(s) trouvé(s)")
        article = pick_next_article(articles, sent_pmids)
        if article:
            print(f"     Résumé : {article['title'][:60]}...")
            summary = summarize_article(article)
            results.append((article, summary))
            new_sent.append(article["pmid"])

    print("  → Envoi de l'email...")
    html = build_email_html(results)
    send_email(html, recipients)

    updated = list(sent_pmids) + new_sent
    memory["sent"] = updated[-500:]
    save_memory(memory)
    print("  ✓ Terminé !")


if __name__ == "__main__":
    main()
