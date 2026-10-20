"""Opens a public https link to this laptop so real WhatsApp (Meta) can reach the agent.
Run:  .venv\Scripts\python tunnel_whatsapp.py      (keep the window open during the demo)
Paste the printed Webhook URL into Meta → WhatsApp → Configuration."""
from pyngrok import ngrok
from app.config import META_VERIFY_TOKEN

t = ngrok.connect(8000, "http")
print("\nWebhook URL :", t.public_url + "/meta/webhook")
print("Verify token:", META_VERIFY_TOKEN)
print("\nKeep this window open. Ctrl+C to stop.")
ngrok.get_ngrok_process().proc.wait()
