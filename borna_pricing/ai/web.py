from __future__ import annotations

from flask import abort, flash, g, redirect, render_template, request, url_for

from .. import fmt
from ..service import ServiceError
from . import content, price_list, reasons, reply
from .gateway import ModelError

MAX_TEXT = 20_000


def register(app, svc, ms, sales, gateway, h) -> None:
    need, uname, can = h.need, h.uname, h.can

    def role():
        return g.user["role"]

    app.jinja_env.globals["ai_ready"] = lambda: gateway.ready
    app.jinja_env.globals["reason_suggestion"] = lambda iid: reasons.pending(svc).get(iid)

    @app.route("/market/price-list", methods=["GET", "POST"])
    @need()
    def market_price_list():
        f = request.form
        form = f.to_dict() if request.method == "POST" else {"unit": "toman", "multiplier": "1", "price_level": "wholesale",
                                                             "channel": "telegram", "tier": "unknown"}
        rows, notes = None, []
        if request.method == "POST" and f.get("action") == "confirm":
            text = (f.get("text") or "")[:MAX_TEXT]
            made, errors = 0, []
            for i in range(int(f.get("n") or 0)):
                if f.get(f"include_{i}") != "1":
                    continue
                price = f.get(f"price_rial_{i}") or ""
                avail = f.get(f"availability_{i}") or "unknown"
                try:
                    ms.manual_observation({"seller": f.get("seller"), "title": f.get(f"title_{i}"), "price": price,
                                           "unit": "rial", "price_level": f.get("price_level"), "tier": f.get("tier"),
                                           "channel": f.get("channel"), "availability": avail,
                                           "product_key": f.get(f"product_{i}") or ""},
                                          uname(), role(), upload=(text.encode("utf-8"), "price-list.txt"))
                    made += 1
                except ServiceError as exc:
                    errors.append(f"{f.get(f'title_{i}')}: {exc}")
            msg = f"{fmt.fa(made)} قیمت ثبت شد" + ("؛ بعد از تأیید در مقایسه‌ها حساب می‌شوند." if role() == "viewer" else ".")
            if errors:
                msg += f" {fmt.fa(len(errors))} ردیف ثبت نشد: " + "؛ ".join(errors)[:400]
            flash(msg, "ok" if not errors else "warn")
            return redirect(url_for("market_price_list"))
        if request.method == "POST":
            text = (f.get("text") or "")[:MAX_TEXT]
            if not (f.get("seller") or "").strip():
                flash("نام رقیب یا کانال را بنویسید.", "error")
            elif len(text.strip()) < 5:
                flash("متن لیست قیمت را بچسبانید.", "error")
            else:
                unit = f.get("unit") if f.get("unit") in ("toman", "rial") else "toman"
                mult = 1000 if f.get("multiplier") == "1000" else 1
                if f.get("multiplier") == "auto":
                    mult = price_list.suggest_multiplier(text)
                    form["multiplier"] = str(mult)
                rows = None
                if f.get("use_model") == "1" and gateway.ready:
                    try:
                        rows = price_list.parse_model(gateway, text, unit, mult, uname())
                    except ModelError as exc:
                        notes.append(f"مدل زبانی جواب نداد ({exc})؛ متن با قاعده خوانده شد.")
                if rows is None:
                    rows = price_list.parse_rules(text, unit, mult)
                rows = price_list.attach_matches(rows, ms.profiles(), ms.cfg.market.length_tolerance_mm)
                if not rows:
                    notes.append("هیچ ردیف قیمت‌داری پیدا نشد.")
        return render_template("market_price_list.html", form=form, rows=rows, notes=notes,
                               products=svc.products(include_inactive=False))

    @app.get("/market/content")
    @need("approver")
    def market_content():
        latest = {}
        for d in content.drafts(svc):
            if d["status"] != "discarded":
                latest.setdefault(d["product_key"], d)
        return render_template("market_content.html", products=svc.products(include_inactive=False), latest=latest)

    @app.post("/market/content/generate")
    @need("approver")
    def market_content_generate():
        try:
            did = content.generate(ms, gateway, request.form.get("product_key", ""), uname(),
                                   use_model=request.form.get("use_model") == "1")
            flash("پیش‌نویس ساخته شد.", "ok")
            return redirect(url_for("market_content_draft", draft_id=did))
        except ServiceError as exc:
            flash(str(exc), "error")
            return redirect(url_for("market_content"))

    @app.get("/market/content/<int:draft_id>")
    @need("approver")
    def market_content_draft(draft_id):
        d = content.draft(svc, draft_id)
        if d is None:
            abort(404)
        return render_template("market_content_draft.html", d=d)

    @app.post("/market/content/<int:draft_id>/decide")
    @need("approver")
    def market_content_decide(draft_id):
        approve = request.form.get("decision") == "approve"
        try:
            content.decide(svc, draft_id, approve, uname())
            flash("متن تأیید شد؛ آن را در آگهی کپی کنید." if approve else "پیش‌نویس کنار گذاشته شد.", "ok")
        except ServiceError as exc:
            flash(str(exc), "error")
        return redirect(url_for("market_content_draft", draft_id=draft_id))

    @app.route("/sales/reply", methods=["GET", "POST"])
    @need()
    def sales_reply():
        result = None
        form = request.form.to_dict()
        if request.method == "POST":
            result = reply.draft(ms, gateway, form.get("question", ""), uname(), use_model=form.get("use_model") == "1")
        return render_template("sales_reply.html", form=form, result=result)

    @app.post("/sales/suggestions/<int:suggestion_id>/decide")
    @need("approver")
    def sales_suggestion_decide(suggestion_id):
        apply = request.form.get("decision") == "apply"
        try:
            reasons.decide(svc, sales, suggestion_id, apply, uname())
            flash("دلیل پیشنهادی ثبت شد." if apply else "پیشنهاد کنار گذاشته شد.", "ok")
        except ServiceError as exc:
            flash(str(exc), "error")
        nxt = request.form.get("next") or ""
        return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("sales_inquiries"))

    @app.route("/market/ai", methods=["GET", "POST"])
    @need("admin")
    def market_ai():
        if request.method == "POST":
            try:
                gateway.complete_json("test", "test-1", 'برای آزمون اتصال فقط این را برگردان: {"ok": true}',
                                      "آزمون", uname())
                flash("مدل زبانی جواب داد و اتصال سالم است.", "ok")
            except ModelError as exc:
                flash(f"آزمون ناموفق بود: {exc}", "error")
            return redirect(url_for("market_ai"))
        return render_template("market_ai.html", llm=svc.cfg.llm, ready=gateway.ready, today=gateway.calls_today(),
                               usage=gateway.usage(), recent=gateway.recent(30))
