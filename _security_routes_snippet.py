

# ==========================================
# SESSION TIMEOUT HOOK
# ==========================================

_SESSION_TIMEOUT_SECONDS = 7200   # 2 hours

@app.before_request
def _enforce_session_timeout():
    """Auto-logout idle sessions."""
    if "user_id" in session:
        login_at = session.get("_login_at")
        if login_at and (int(time.time()) - login_at) > _SESSION_TIMEOUT_SECONDS:
            session.clear()
            flash("Your session has expired. Please log in again.", "warning")
            return redirect(url_for("login"))


# ==========================================
# SECURITY ADMIN ROUTES
# ==========================================

def _admin_required_security(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get("role") != "admin":
            flash("Administrator access required.", "danger")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


@app.route("/admin/security", methods=["GET"])
@_admin_required_security
def admin_security_dashboard():
    db = get_db()
    _ensure_security_schema(db)
    try:
        all_users = db.execute(
            "SELECT id, username, role, last_login, is_active, failed_attempts, locked_until FROM users ORDER BY username"
        ).fetchall()
        recent_activity = db.execute(
            "SELECT * FROM audit_logs ORDER BY id DESC LIMIT 50"
        ).fetchall()
        failed_logins = db.execute(
            "SELECT * FROM login_attempts WHERE success=0 ORDER BY id DESC LIMIT 30"
        ).fetchall()
        role_counts = {}
        for u in all_users:
            r = row_get(u, "role") or "unknown"
            role_counts[r] = role_counts.get(r, 0) + 1
        total_users    = len(all_users)
        active_users   = sum(1 for u in all_users if row_get(u, "is_active") in (1, None, "1"))
        locked_users   = sum(1 for u in all_users if row_get(u, "locked_until"))
        disabled_users = sum(1 for u in all_users if row_get(u, "is_active") == 0)
        total_failed_row = db.execute("SELECT COUNT(*) AS c FROM login_attempts WHERE success=0").fetchone()
        total_failed_count = row_get(total_failed_row, "c") or 0
        return render_template(
            "admin_security_dashboard.html",
            all_users=all_users,
            recent_activity=recent_activity,
            failed_logins=failed_logins,
            role_counts=role_counts,
            total_users=total_users,
            active_users=active_users,
            locked_users=locked_users,
            disabled_users=disabled_users,
            total_failed_count=total_failed_count,
        )
    except Exception as e:
        print(f"[admin_security_dashboard ERROR]: {repr(e)}")
        flash(f"Error loading security dashboard: {str(e)}", "error")
        return redirect(url_for("dashboard"))
    finally:
        if db:
            try: db.close()
            except: pass


@app.route("/admin/security/users", methods=["GET"])
@_admin_required_security
def admin_user_management():
    db = get_db()
    _ensure_security_schema(db)
    ph = get_placeholder()
    search_q    = request.args.get("q", "").strip()
    role_filter = request.args.get("role", "").strip()
    try:
        sql    = "SELECT id, username, role, last_login, is_active, failed_attempts, locked_until FROM users WHERE 1=1"
        params = []
        if search_q:
            sql += f" AND LOWER(username) LIKE {ph}"
            params.append(f"%{search_q.lower()}%")
        if role_filter:
            sql += f" AND role = {ph}"
            params.append(role_filter)
        sql += " ORDER BY username ASC"
        users = db.execute(sql, tuple(params)).fetchall()
        return render_template("admin_user_management.html", users=users, search_q=search_q, role_filter=role_filter)
    except Exception as e:
        flash(f"Error loading users: {str(e)}", "error")
        return redirect(url_for("admin_security_dashboard"))
    finally:
        if db:
            try: db.close()
            except: pass


@app.route("/admin/security/users/create", methods=["GET", "POST"])
@_admin_required_security
def admin_create_user():
    db = get_db()
    _ensure_security_schema(db)
    ph = get_placeholder()
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = (request.form.get("password") or "").strip()
        role     = (request.form.get("role") or "").strip()
        if not username or not password or not role:
            flash("Username, password and role are required.", "error")
            return redirect(url_for("admin_create_user"))
        if len(password) < 8 or not re.search(r"\d", password):
            flash("Password must be at least 8 characters and include a number.", "error")
            return redirect(url_for("admin_create_user"))
        try:
            db.execute(
                f"INSERT INTO users (username, password, role, is_active) VALUES ({ph},{ph},{ph},1)",
                (username, generate_password_hash(password), role),
            )
            db.commit()
            _log_audit(db, "USER_CREATED", entity="users", detail=f"username={username} role={role}")
            flash(f"User '{username}' created with role '{role}'.", "success")
            return redirect(url_for("admin_user_management"))
        except Exception as e:
            flash(f"Error creating user: {str(e)}", "error")
            return redirect(url_for("admin_create_user"))
        finally:
            if db:
                try: db.close()
                except: pass
    if db:
        try: db.close()
        except: pass
    return render_template("admin_create_user.html")


@app.route("/admin/security/users/<int:user_id>/toggle", methods=["POST"])
@_admin_required_security
def admin_toggle_user(user_id):
    db = get_db()
    _ensure_security_schema(db)
    ph = get_placeholder()
    try:
        row = db.execute(f"SELECT username, is_active FROM users WHERE id={ph}", (user_id,)).fetchone()
        if not row:
            flash("User not found.", "error")
            return redirect(url_for("admin_user_management"))
        username   = row_get(row, "username")
        cur_active = row_get(row, "is_active")
        new_active = 0 if (cur_active in (1, None, "1")) else 1
        db.execute(f"UPDATE users SET is_active={ph} WHERE id={ph}", (new_active, user_id))
        db.commit()
        action_word = "DISABLED" if new_active == 0 else "ENABLED"
        _log_audit(db, f"USER_{action_word}", entity="users", entity_id=user_id, detail=f"username={username}")
        flash(f"User '{username}' {'disabled' if new_active==0 else 're-enabled'}.", "success")
    except Exception as e:
        flash(f"Error: {str(e)}", "error")
    finally:
        if db:
            try: db.close()
            except: pass
    return redirect(url_for("admin_user_management"))


@app.route("/admin/security/users/<int:user_id>/reset-password", methods=["POST"])
@_admin_required_security
def admin_reset_password(user_id):
    db = get_db()
    _ensure_security_schema(db)
    ph = get_placeholder()
    new_password = (request.form.get("new_password") or "").strip()
    if len(new_password) < 8 or not re.search(r"\d", new_password):
        flash("Password must be >= 8 chars and include a digit.", "error")
        return redirect(url_for("admin_user_management"))
    try:
        row = db.execute(f"SELECT username FROM users WHERE id={ph}", (user_id,)).fetchone()
        if not row:
            flash("User not found.", "error")
            return redirect(url_for("admin_user_management"))
        username = row_get(row, "username")
        db.execute(
            f"UPDATE users SET password={ph}, failed_attempts=0, locked_until=NULL WHERE id={ph}",
            (generate_password_hash(new_password), user_id),
        )
        db.commit()
        _log_audit(db, "PASSWORD_RESET", entity="users", entity_id=user_id, detail=f"username={username}")
        flash(f"Password for '{username}' reset.", "success")
    except Exception as e:
        flash(f"Error: {str(e)}", "error")
    finally:
        if db:
            try: db.close()
            except: pass
    return redirect(url_for("admin_user_management"))


@app.route("/admin/security/users/<int:user_id>/change-role", methods=["POST"])
@_admin_required_security
def admin_change_role(user_id):
    db = get_db()
    _ensure_security_schema(db)
    ph = get_placeholder()
    new_role = (request.form.get("new_role") or "").strip()
    if new_role not in ("admin", "accountant", "teacher", "student", "parent"):
        flash("Invalid role.", "error")
        return redirect(url_for("admin_user_management"))
    try:
        row = db.execute(f"SELECT username, role FROM users WHERE id={ph}", (user_id,)).fetchone()
        if not row:
            flash("User not found.", "error")
            return redirect(url_for("admin_user_management"))
        username = row_get(row, "username")
        old_role = row_get(row, "role")
        db.execute(f"UPDATE users SET role={ph} WHERE id={ph}", (new_role, user_id))
        db.commit()
        _log_audit(db, "ROLE_CHANGED", entity="users", entity_id=user_id,
                   detail=f"username={username} {old_role}->{new_role}")
        flash(f"Role for '{username}' changed from '{old_role}' to '{new_role}'.", "success")
    except Exception as e:
        flash(f"Error: {str(e)}", "error")
    finally:
        if db:
            try: db.close()
            except: pass
    return redirect(url_for("admin_user_management"))


@app.route("/admin/security/users/<int:user_id>/unlock", methods=["POST"])
@_admin_required_security
def admin_unlock_user(user_id):
    db = get_db()
    _ensure_security_schema(db)
    ph = get_placeholder()
    try:
        row = db.execute(f"SELECT username FROM users WHERE id={ph}", (user_id,)).fetchone()
        if not row:
            flash("User not found.", "error")
            return redirect(url_for("admin_user_management"))
        username = row_get(row, "username")
        db.execute(f"UPDATE users SET locked_until=NULL, failed_attempts=0 WHERE id={ph}", (user_id,))
        db.commit()
        _log_audit(db, "ACCOUNT_UNLOCKED", entity="users", entity_id=user_id, detail=f"username={username}")
        flash(f"Account '{username}' unlocked.", "success")
    except Exception as e:
        flash(f"Error: {str(e)}", "error")
    finally:
        if db:
            try: db.close()
            except: pass
    return redirect(url_for("admin_user_management"))


@app.route("/admin/security/audit-log", methods=["GET"])
@_admin_required_security
def admin_audit_log():
    db = get_db()
    _ensure_security_schema(db)
    ph = get_placeholder()
    actor_filter  = request.args.get("actor", "").strip()
    action_filter = request.args.get("action", "").strip()
    date_from     = request.args.get("date_from", "").strip()
    date_to       = request.args.get("date_to", "").strip()
    try:
        sql    = "SELECT * FROM audit_logs WHERE 1=1"
        params = []
        if actor_filter:
            sql += f" AND LOWER(actor_username) LIKE {ph}"
            params.append(f"%{actor_filter.lower()}%")
        if action_filter:
            sql += f" AND action LIKE {ph}"
            params.append(f"%{action_filter}%")
        if date_from:
            sql += f" AND DATE(created_at) >= {ph}"
            params.append(date_from)
        if date_to:
            sql += f" AND DATE(created_at) <= {ph}"
            params.append(date_to)
        sql += " ORDER BY id DESC LIMIT 200"
        logs = db.execute(sql, tuple(params)).fetchall()
        return render_template(
            "admin_audit_log.html",
            logs=logs, actor_filter=actor_filter,
            action_filter=action_filter,
            date_from=date_from, date_to=date_to,
        )
    except Exception as e:
        flash(f"Error loading audit log: {str(e)}", "error")
        return redirect(url_for("admin_security_dashboard"))
    finally:
        if db:
            try: db.close()
            except: pass


@app.route("/admin/security/failed-logins", methods=["GET"])
@_admin_required_security
def admin_failed_logins():
    db = get_db()
    _ensure_security_schema(db)
    try:
        rows = db.execute(
            "SELECT * FROM login_attempts WHERE success=0 ORDER BY id DESC LIMIT 100"
        ).fetchall()
        return render_template("admin_failed_logins.html", rows=rows)
    except Exception as e:
        flash(f"Error: {str(e)}", "error")
        return redirect(url_for("admin_security_dashboard"))
    finally:
        if db:
            try: db.close()
            except: pass
