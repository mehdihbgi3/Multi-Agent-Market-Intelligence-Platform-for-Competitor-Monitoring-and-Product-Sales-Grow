from __future__ import annotations

import os
import secrets
import time
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path

from flask import abort, flash, g, redirect, render_template, request, send_file, session, url_for

from .. import fmt
from ..clock import now_utc, parse_iso, tehran_date
from ..market import textnorm as tn
from ..service import ServiceError
from . import advisor, credit, orders_xlsx
from .service import (CUSTOMER_KINDS, INQUIRY_STATUS, LEAD_KINDS, LEAD_STATUS, LOST_REASONS, PHONE_FILTERS,
                      SALES_CHANNELS, link_label, parse_rial)

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
LEADS_SHOWN = 500


def globals_for_templates() -> dict:
    return {"CUSTOMER_KINDS": CUSTOMER_KINDS, "SALES_CHANNELS": SALES_CHANNELS, "LOST_REASONS": LOST_REASONS,
            "INQUIRY_STATUS": INQUIRY_STATUS, "LEAD_STATUS": LEAD_STATUS, "LEAD_KINDS": LEAD_KINDS, "ADVISOR_ACTIONS": advisor.ACTIONS,
            "PHONE_FILTERS": PHONE_FILTERS, "link_label": link_label}


def _pct_value(rate) -> str:
    if rate is None:
        return ""
    return format((rate * 100).normalize(), "f")


def _rate_input(text, default):
    s = tn.latin_digits(text or "").replace("٪", "").replace("%", "").replace("٫", ".").strip()
    if not s:
        return default
    try:
        v = Decimal(s) / 100
    except InvalidOperation:
        raise ServiceError(f"«{text}» درصد معتبر نیست.") from None
    if not v.is_finite() or v < -1 or v > 10:
        raise ServiceError("درصد باید بین −۱۰۰ و ۱۰۰۰ باشد.")
    return v


def register(app, svc, ms, sales, h) -> None:
    need, uname, can = h.need, h.uname, h.can
    uploads = Path(svc.cfg.paths.database).parent / "uploads"

    def role():
        return g.user["role"]

    def guarded(fn, ok_msg, endpoint, **kw):
        try:
            res = fn()
            flash(ok_msg(res) if callable(ok_msg) else ok_msg, "ok")
        except ServiceError as exc:
            flash(str(exc), "error")
        nxt = request.form.get("next") or ""
        if nxt.startswith("/") and not nxt.startswith("//"):
            return redirect(nxt)
        return redirect(url_for(endpoint, **kw))

    def products():
        return svc.products(include_inactive=False)

    @app.get("/sales")
    @need()
    def sales_dashboard():
        days = request.args.get("days", "90")
        days = int(days) if days in ("30", "90", "180", "365") else 90
        return render_template("sales_dashboard.html", k=sales.kpis(days), f=sales.followups(), days=days)

    @app.get("/sales/inquiries")
    @need()
    def sales_inquiries():
        status = request.args.get("status") or None
        return render_template("sales_inquiries.html", rows=sales.inquiries(status), status=status)

    @app.route("/sales/inquiries/new", methods=["GET", "POST"])
    @need()
    def sales_inquiry_new():
        form = request.form.to_dict() if request.method == "POST" else {"customer_id": request.args.get("customer", "")}
        if request.method == "POST":
            try:
                iid = sales.create_inquiry(form, uname())
                flash("استعلام ثبت شد.", "ok")
                return redirect(url_for("sales_inquiry", inquiry_id=iid))
            except ServiceError as exc:
                flash(str(exc), "error")
        return render_template("sales_inquiry_new.html", form=form, customers=sales.customers(False),
                               products=products(), official=sales.official_prices())

    @app.get("/sales/inquiries/<int:inquiry_id>")
    @need()
    def sales_inquiry(inquiry_id):
        inq = sales.inquiry(inquiry_id)
        if inq is None:
            abort(404)
        return render_template("sales_inquiry.html", i=inq)

    @app.post("/sales/inquiries/<int:inquiry_id>/close")
    @need()
    def sales_inquiry_close(inquiry_id):
        outcome = request.form.get("outcome", "")

        def msg(res):
            if outcome == "won":
                return "استعلام به فروش رسید" + ("؛ سفارش هم ثبت شد." if res["order_id"] else ".")
            return "استعلام بسته شد" + ("؛ قیمت رقیب برای بررسی به بخش بازار رفت." if res["observation_id"] else ".")
        return guarded(lambda: sales.close_inquiry(inquiry_id, outcome, uname(), role(), request.form.to_dict()), msg,
                       "sales_inquiry", inquiry_id=inquiry_id)

    @app.post("/sales/inquiries/<int:inquiry_id>/follow-up")
    @need()
    def sales_inquiry_follow_up(inquiry_id):
        return guarded(lambda: sales.set_follow_up(inquiry_id, request.form.get("follow_up_on", ""), uname()),
                       "تاریخ پیگیری ثبت شد.", "sales_inquiry", inquiry_id=inquiry_id)

    @app.post("/sales/inquiries/<int:inquiry_id>/reason")
    @need("approver")
    def sales_inquiry_reason(inquiry_id):
        return guarded(lambda: sales.set_lost_reason(inquiry_id, request.form.get("lost_reason", ""), uname()),
                       "دلیل ثبت شد.", "sales_inquiry", inquiry_id=inquiry_id)

    @app.get("/sales/customers")
    @need()
    def sales_customers():
        return render_template("sales_customers.html", rows=sales.customers())

    @app.route("/sales/customers/new", methods=["GET", "POST"])
    @need()
    def sales_customer_new():
        form = request.form.to_dict() if request.method == "POST" else {"kind": "wholesaler"}
        if request.method == "POST":
            try:
                cid = sales.save_customer(form, uname())
                flash("مشتری ثبت شد.", "ok")
                return redirect(url_for("sales_customer", customer_id=cid))
            except ServiceError as exc:
                flash(str(exc), "error")
        return render_template("sales_customer.html", c=None, form=form)

    @app.route("/sales/customers/<int:customer_id>", methods=["GET", "POST"])
    @need()
    def sales_customer(customer_id):
        c = sales.customer(customer_id)
        if c is None:
            abort(404)
        form = dict(c)
        if request.method == "POST":
            form = request.form.to_dict()
            try:
                sales.save_customer(form, uname(), customer_id)
                flash("تغییرات مشتری ذخیره شد.", "ok")
                return redirect(url_for("sales_customer", customer_id=customer_id))
            except ServiceError as exc:
                flash(str(exc), "error")
        return render_template("sales_customer.html", c=c, form=form)

    @app.get("/sales/orders")
    @need()
    def sales_orders():
        return render_template("sales_orders.html", rows=sales.orders())

    @app.route("/sales/orders/new", methods=["GET", "POST"])
    @need()
    def sales_order_new():
        form = request.form.to_dict() if request.method == "POST" else {"customer_id": request.args.get("customer", "")}
        if request.method == "POST":
            lines = [{"product_key": request.form.get(f"product_{i}"), "qty": request.form.get(f"qty_{i}"),
                      "unit_price": request.form.get(f"price_{i}")} for i in range(1, 7)]
            try:
                oid = sales.create_order(form, lines, uname())
                flash(f"سفارش شماره {fmt.fa(oid)} ثبت شد.", "ok")
                return redirect(url_for("sales_orders"))
            except ServiceError as exc:
                flash(str(exc), "error")
        return render_template("sales_order_new.html", form=form, customers=sales.customers(False), products=products(),
                               official=sales.official_prices())

    @app.get("/sales/orders/template.xlsx")
    @need()
    def sales_orders_template():
        return send_file(BytesIO(orders_xlsx.template(products())), as_attachment=True,
                         download_name="Borna_Orders_Template.xlsx", mimetype=XLSX)

    @app.post("/sales/orders/import")
    @need("approver")
    def sales_orders_import():
        f = request.files.get("file")
        if not f or not f.filename.lower().endswith(".xlsx"):
            flash("یک فایل Excel با پسوند xlsx انتخاب کنید.", "error")
            return redirect(url_for("sales_orders"))
        uploads.mkdir(parents=True, exist_ok=True)
        for old in uploads.glob("orders_*.xlsx"):
            if time.time() - old.stat().st_mtime > 86400:
                old.unlink(missing_ok=True)
        token = secrets.token_hex(12)
        path = uploads / f"orders_{token}.xlsx"
        f.save(path)
        try:
            rows, errors = orders_xlsx.read(path, {p["key"] for p in products()})
        except ServiceError as exc:
            path.unlink(missing_ok=True)
            flash(str(exc), "error")
            return redirect(url_for("sales_orders"))
        session["orders_import"] = token
        return render_template("sales_orders_import.html", rows=rows, errors=errors, token=token,
                               name=os.path.basename(f.filename)[:120])

    @app.post("/sales/orders/import/apply")
    @need("approver")
    def sales_orders_import_apply():
        token = request.form.get("token", "")
        if not token or token != session.get("orders_import"):
            flash("پیش‌نمایش منقضی شده؛ فایل را دوباره بارگذاری کنید.", "error")
            return redirect(url_for("sales_orders"))
        path = uploads / f"orders_{token}.xlsx"
        try:
            rows, _ = orders_xlsx.read(path, {p["key"] for p in products()})
            res = sales.import_orders(rows, uname())
            msg = f"{fmt.fa(res['orders'])} سفارش با {fmt.fa(res['lines'])} ردیف وارد شد."
            if res["skipped"]:
                msg += f" {fmt.fa(len(res['skipped']))} سفارش وارد نشد: " + "؛ ".join(res["skipped"])[:400]
            flash(msg, "ok" if not res["skipped"] else "warn")
        except ServiceError as exc:
            flash(str(exc), "error")
        finally:
            path.unlink(missing_ok=True)
            session.pop("orders_import", None)
        return redirect(url_for("sales_orders"))

    @app.get("/sales/credit")
    @need()
    def sales_credit():
        a = request.args
        cfgc = svc.cfg.credit
        form = {"product_key": a.get("product_key", ""), "qty": a.get("qty", ""), "cash": a.get("cash", ""),
                "days": a.get("days", str(cfgc.check_days[1] if len(cfgc.check_days) > 1 else cfgc.check_days[0])),
                "rate": a.get("rate", _pct_value(cfgc.annual_cost_of_money)),
                "dollar": a.get("dollar", _pct_value(cfgc.annual_dollar_change)),
                "offered": a.get("offered", "")}
        result = error = None
        if a.get("go"):
            try:
                official = sales.official_prices()
                if form["product_key"]:
                    if form["product_key"] not in official:
                        raise ServiceError("کالا در لیست رسمی نیست.")
                    qty = int(tn.latin_digits(form["qty"] or "1").strip() or 1)
                    cash = official[form["product_key"]] * max(qty, 1)
                else:
                    cash = parse_rial(form["cash"], "toman", "مبلغ نقد")
                days = int(tn.latin_digits(form["days"]).strip())
                r = _rate_input(form["rate"], None)
                if r is None:
                    raise ServiceError("نرخ سالانه هزینه پول را وارد کنید (یا مالی آن را در تنظیمات بگذارد).")
                result = credit.quote(cash, days, r, _rate_input(form["dollar"], None),
                                      parse_rial(form["offered"], "toman", "مبلغ چک پیشنهادی", required=False))
            except (ServiceError, ValueError) as exc:
                error = str(exc)
        return render_template("sales_credit.html", form=form, result=result, error=error, products=products(),
                               official=sales.official_prices())

    @app.get("/sales/advisor")
    @need("approver")
    def sales_advisor():
        reps = ms.reports(5, "advisor")
        rep = ms.report(reps[0]["id"]) if reps else None
        return render_template("sales_advisor.html", rep=rep, costs=sales.costs(), products=products(),
                               min_margin=svc.cfg.advisor.min_margin)

    @app.post("/sales/advisor/run")
    @need("approver")
    def sales_advisor_run():
        return guarded(lambda: ms.run_agent("pricing_advisor", "manual", uname()),
                       lambda r: f"مشاور قیمت اجرا شد: {r['status']}" + (f" — {r['message']}" if r.get("message") else ""),
                       "sales_advisor")

    @app.get("/sales/advisor/<int:report_id>.xlsx")
    @need("approver")
    def sales_advisor_xlsx(report_id):
        rep = ms.report(report_id)
        if rep is None or rep["kind"] != "advisor":
            abort(404)
        stamp = parse_iso(rep["created_at"])
        return send_file(BytesIO(advisor.xlsx(rep["data"], svc.cfg.web.company_name)), as_attachment=True,
                         download_name=f"Borna_Price_Advice_{report_id}_{stamp:%Y%m%d}.xlsx", mimetype=XLSX)

    @app.post("/sales/costs")
    @need("approver")
    def sales_costs():
        return guarded(lambda: sales.set_cost(request.form.get("product_key", ""), request.form.get("cost_usd", ""),
                                              uname(), request.form.get("note", "")),
                       "بهای تمام‌شده ثبت شد.", "sales_advisor")

    @app.get("/sales/leads")
    @need()
    def sales_leads():
        status = request.args.get("status") or None
        kind = request.args.get("kind") or None
        city = request.args.get("city") or None
        q = (request.args.get("q") or "").strip()[:60] or None
        phone = request.args.get("phone") if request.args.get("phone") in PHONE_FILTERS else None
        return render_template("sales_leads.html", rows=sales.leads(status, kind, city, q, phone), status=status,
                               kind=kind, city=city, q=q, phone=phone, cities=sales.lead_cities(), shown=LEADS_SHOWN)

    @app.get("/sales/leads.xlsx")
    @need()
    def sales_leads_xlsx():
        data, _ = sales.leads_workbook(request.args.get("status") or None, request.args.get("kind") or None,
                                       request.args.get("city") or None, uname(), request.args.get("q") or None,
                                       request.args.get("phone") or None)
        return send_file(BytesIO(data), as_attachment=True, mimetype=XLSX,
                         download_name=f"Borna_Businesses_{tehran_date(now_utc()):%Y%m%d}.xlsx")

    @app.post("/sales/leads/phones")
    @need()
    def sales_leads_phones():
        f = request.files.get("file")
        if not f or not f.filename.lower().endswith(".xlsx"):
            flash("فایل Excel (با پسوند xlsx) را که «دریافت Excel» ساخته است انتخاب کنید.", "error")
            return redirect(url_for("sales_leads"))

        def msg(r):
            text = (f"{fmt.fa(r['changed'])} تلفن تازه ثبت شد؛ {fmt.fa(r['same'])} ردیف تغییری نداشت"
                    + (f"؛ {fmt.fa(r['unknown'])} شناسه در سامانه نبود" if r["unknown"] else "") + ".")
            if r["errors"]:
                text += (f" {fmt.fa(len(r['errors']))} ردیف خوانده نشد: " + " ".join(r["errors"][:5])
                         + (" …" if len(r["errors"]) > 5 else ""))
            return text
        return guarded(lambda: sales.import_lead_phones(f.read(), f.filename, uname()), msg, "sales_leads")

    @app.post("/sales/leads/refresh")
    @need()
    def sales_leads_refresh():
        return guarded(lambda: sales.refresh_leads(),
                       lambda r: f"{fmt.fa(r['new'])} مشتری احتمالی تازه از {fmt.fa(r['sellers'])} فروشنده.", "sales_leads")

    @app.post("/sales/leads/<int:lead_id>/status")
    @need()
    def sales_lead_status(lead_id):
        return guarded(lambda: sales.set_lead_status(lead_id, request.form.get("status", ""), uname(),
                                                     request.form.get("note", ""), request.form.get("phone", "")),
                       "ثبت شد.", "sales_leads")

    @app.post("/sales/leads/<int:lead_id>/customer")
    @need()
    def sales_lead_customer(lead_id):
        try:
            cid = sales.lead_to_customer(lead_id, uname(), request.form.get("kind") or None)
            flash("مشتری ساخته شد.", "ok")
            return redirect(url_for("sales_customer", customer_id=cid))
        except ServiceError as exc:
            flash(str(exc), "error")
            return redirect(url_for("sales_leads"))

