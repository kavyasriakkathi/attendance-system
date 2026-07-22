from app import app
from flask import render_template
with app.app_context():
    try:
        render_template("teacher_login.html", hide_nav=True)
        print("Template rendered successfully.")
    except Exception as e:
        print("Template failed:", e)
