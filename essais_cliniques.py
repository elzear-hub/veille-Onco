"""
Veille essais cliniques en cancérologie v5
- Subscribers depuis subscribers.json
- Ton pédagogique rigoureux pour M2
- Source : RSS officiel ClinicalTrials.gov
"""

import requests
import smtplib
import json
import os
import xml.etree.ElementTree as ET
import base64
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ─── CONFIGURATION ────────────────────────────────────────────────────────────
GROQ_API_KEY   = os.environ["GROQ_API_KEY"]
GMAIL_ADDRESS  = os.environ["GMAIL_ADDRESS"]
GMAIL_PASSWORD = os.environ["GMAIL_PASSWORD"]
GITHUB_TOKEN   = os.environ["GITHUB_TOKEN"]
GITHUB_REPO    = os.environ["GITHUB_REPO"]

MAX_ESSAIS = 5
MEMORY_FILE = "sent_trials.json"
SUBSCRIBERS_FILE = "subscribers.json"

RSS_URL = "https://clinicaltrials.gov/api/rss?cond=cancer&aggFilters=phase%3A1+2%2Cstatus%3Arec&dateField=StudyFirstPostDate"
# ─────────────────────────────────────────────────────────────────────────────


# ── GitHub helpers ────────────────────────────────────────────────────────────

def github_get(filename):
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}"}
    r = requests.get(url, headers=headers, timeout=15)
    if r.status_code == 404:
        return None, None
    r.raise_for_status()
    content = base64.b64decode(r.json()["content"]).decode("utf-8")
    return json.loads(content), r.json()["sha"]


def github_put(filename, data, sha, message):
    content = base64.b64encode(json.dumps(data, indent=2).encode()).decode()
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Content-Type": "application/json"}
    payload = {"message": message, "content": content}
    if sha:
        payload["sha"] = sha
    requests.put(url, headers=headers, json=payload, timeout=15).raise_for_status()


def load_subscribers():
    data, _ = github_get(SUBSCRIBERS_FILE)
    return data.get("emails", []) if data else []


def load_memory():
    data, sha = github_get(MEMORY_FILE)
    if not data:
        return {"sent": [], "_sha": None}
    data["_sha"] = sha
    return data


def save_memory(memory):
    sha = memory.pop("_sha", None)
    github_put(MEMORY_FILE, memory, sha,
               f"Mise à jour mémoire essais {datetime.now().strftime('%Y-%m-%d')}")


# ── RSS ───────────────────────────────────────────────────────────────────────

def fetch_trials():
    r = requests.get(RSS_URL, timeout=20)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    channel = root.find("channel")
    if channel is None:
        return []

    trials = []
    seen = set()
    for item in channel.findall("item"):
        title = item.findtext("title", "").strip()
        link = item.findtext("link", "").strip()
        description = item.findtext("description", "").strip()

        nct_id = ""
        if "NCT" in link:
            for p in link.split("/"):
                if p.startswith("NCT"):
                    nct_id = p.split("?")[0]
                    break

        if not nct_id or nct_id in seen:
            continue
        seen.add(nct_id)

        trials.append({
            "nct_id": nct_id, "title": title,
            "description": description[:1500],
            "url": f"https://clinicaltrials.gov/study/{nct_id}",
        })
    return trials


def pick_new_trials(trials, sent_ids):
    return [t for t in trials if t["nct_id"] not in sent_ids][:MAX_ESSAIS]


# ── Groq ──────────────────────────────────────────────────────────────────────

def summarize_trial(trial):
    prompt = f"""Tu es un enseignant-chercheur en oncologie. Présente cet essai clinique de façon pédagogique à des étudiants en Master 2 de cancérologie.

Le résumé doit être rigoureux, utiliser la terminologie scientifique appropriée, mais rester formatif et accessible. Replace l'essai dans son contexte scientifique.

Titre : {trial['title']}
Description : {trial['description']}

Format attendu en français :
🔬 CONTEXTE : (pourquoi cet essai est important, quelle question scientifique il adresse)
🎯 OBJECTIF : (hypothesis principale et critère de jugement primaire)
💊 INTERVENTION : (traitement testé, mécanisme d'action si pertinent)
👥 POPULATION : (type de cancer, stade, critères d'inclusion principaux)
📊 DESIGN : (phase, nombre de patients, méthodologie)
🧠 ENJEU SCIENTIFIQUE : (ce que ce résultat pourrait apporter à la compréhension ou au traitement)"""

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

def build_email_html(trials_with_summaries):
    date_str = datetime.now().strftime("%d %B %Y")
    count = len(trials_with_summaries)
    cards = ""

    for trial, summary in trials_with_summaries:
        summary_html = summary.replace("\n", "<br>")
        cards += f"""
        <div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:12px;
                    padding:24px;margin-bottom:20px;">
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:12px;flex-wrap:wrap;">
            <span style="background:#EEF2FF;color:#4338CA;font-size:12px;font-weight:600;
                         padding:3px 10px;border-radius:20px;">Phase 1-2</span>
            <span style="background:#FEF3C7;color:#92400E;font-size:12px;font-weight:600;
                         padding:3px 10px;border-radius:20px;">Recruiting</span>
            <span style="color:#9ca3af;font-size:12px;">{trial['nct_id']}</span>
          </div>
          <h3 style="margin:0 0 14px;font-size:15px;color:#111827;line-height:1.5;">
            {trial['title']}
          </h3>
          <div style="font-size:14px;color:#374151;line-height:1.9;">
            {summary_html}
          </div>
          <a href="{trial['url']}"
             style="display:inline-block;margin-top:14px;font-size:13px;
                    color:#4F46E5;text-decoration:none;">
            → Voir l'essai complet sur ClinicalTrials.gov
          </a>
        </div>"""

    if not trials_with_summaries:
        cards = """<div style="text-align:center;padding:40px;color:#6b7280;">
            Aucun nouvel essai trouvé cette semaine.</div>"""

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  <div style="max-width:640px;margin:0 auto;padding:32px 16px;">
    <div style="text-align:center;margin-bottom:28px;">
      <h1 style="font-size:22px;color:#111827;margin:0 0 6px;">🧪 Essais Cliniques Oncologie</h1>
      <p style="color:#6b7280;font-size:14px;margin:0;">
        Semaine du {date_str} · {count} essai(s) phase 1-2 · ClinicalTrials.gov
      </p>
    </div>
    {cards}
    <div style="text-align:center;margin-top:28px;color:#9ca3af;font-size:12px;">
      Veille automatique M2 Oncologie · Résumés par IA · Toujours vérifier la source originale
    </div>
  </div>
</body></html>"""


def send_email(html_content, recipients):
    if not recipients:
        print("  ⚠ Aucun subscriber !")
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🧪 Essais Cliniques Oncologie M2 — {datetime.now().strftime('%d/%m/%Y')}"
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html_content, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, recipients, msg.as_string())
    print(f"  ✓ Email envoyé à {len(recipients)} destinataire(s)")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Démarrage veille essais v5...")

    print("  → Chargement des subscribers...")
    recipients = load_subscribers()
    print(f"  → {len(recipients)} destinataire(s)")

    print("  → Chargement de la mémoire...")
    memory = load_memory()
    sent_ids = set(memory.get("sent", []))

    print("  → Récupération des essais via RSS...")
    trials = fetch_trials()
    print(f"  → {len(trials)} essai(s) trouvé(s)")

    new_trials = pick_new_trials(trials, sent_ids)
    print(f"  → {len(new_trials)} nouvel(s) essai(s) sélectionné(s)")

    results = []
    for i, trial in enumerate(new_trials, 1):
        print(f"  → Résumé {i}/{len(new_trials)} : {trial['title'][:60]}...")
        summary = summarize_trial(trial)
        results.append((trial, summary))

    print("  → Envoi de l'email...")
    html = build_email_html(results)
    send_email(html, recipients)

    updated = list(sent_ids) + [t["nct_id"] for t in new_trials]
    memory["sent"] = updated[-1000:]
    save_memory(memory)
    print("  ✓ Terminé !")


if __name__ == "__main__":
    main()
