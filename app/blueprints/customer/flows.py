# app/blueprints/customer/flows.py
from __future__ import annotations

import json
import logging
from flask import render_template, redirect, url_for, flash, request, jsonify, g
from flask_login import current_user

from app.blueprints.customer import customer_bp
from app.blueprints.auth.decorators import require_login, require_role
from app.models.tenant import get_tenant_by_id, update_tenant_settings
from app.db import db
from app.services.tts_service import generate_order_audio
from call_engine.audio import create_uploaded_audio

logger = logging.getLogger("ccall")


def build_prompt_audio(tenant_id: int) -> str:
    """Create a node prompt from upload or TTS text. Returns basename or ''."""
    upload = request.files.get("prompt_file")
    if upload and upload.filename:
        return create_uploaded_audio(upload)
    text = request.form.get("prompt_text", "").strip()
    if text:
        lang = request.form.get("prompt_lang", "bn").strip() or "bn"
        return generate_order_audio(tenant_id, text, settings_override={"tts_lang": lang})
    return ""


@customer_bp.route("/flows", methods=["GET"])
@require_login
@require_role(["admin", "agent"])
def flows_list():
    tenant_id = g.tenant_id
    rows = db.query(
        """
        SELECT f.*, 
               (SELECT COUNT(*) FROM flow_nodes n WHERE n.flow_id = f.id) AS node_count 
          FROM flows f 
         WHERE f.tenant_id = %s 
      ORDER BY f.created_at DESC
        """,
        (tenant_id,)
    )
    return render_template("customer/flows.html", flows=rows)


@customer_bp.route("/flows/new", methods=["POST"])
@require_login
@require_role(["admin"])
def flow_new():
    tenant_id = g.tenant_id
    
    # Check flow plan limits
    plan = db.query_one(
        "SELECT p.max_flows FROM tenants t JOIN plans p ON t.plan_id = p.id WHERE t.id = %s",
        (tenant_id,)
    )
    max_flows = plan["max_flows"] if plan else 1

    current_flows_count = db.query_one(
        "SELECT COUNT(*) FROM flows WHERE tenant_id = %s",
        (tenant_id,)
    )
    current_flows_count = int(current_flows_count["count"]) if current_flows_count else 0

    if current_flows_count >= max_flows:
        flash(f"Your plan limits you to {max_flows} voice flows. Please upgrade your plan.", "error")
        return redirect(url_for("customer.flows_list"))

    name = request.form.get("name", "").strip() or "Untitled flow"
    description = request.form.get("description", "").strip()
    flow_id = db.insert_returning_id(
        "INSERT INTO flows (name, description, tenant_id) VALUES (%s, %s, %s) RETURNING id",
        (name, description, tenant_id),
    )
    flash("Flow created. Add menu nodes below.", "success")
    return redirect(url_for("customer.flow_edit", flow_id=flow_id))


def _add_demo_node(flow_id: int, name: str, node_type: str, prompt_text: str, tenant_id: int,
                   *, transfer_number: str = "", timeout: int = 8,
                   retries: int = 2, audio_errors: list[str] | None = None) -> int:
    prompt_audio = ""
    if prompt_text:
        try:
            prompt_audio = generate_order_audio(tenant_id, prompt_text, settings_override={"tts_lang": "bn"})
        except Exception as exc:
            if audio_errors is not None:
                audio_errors.append(f"{name}: {exc}")
    return db.insert_returning_id(
        """
        INSERT INTO flow_nodes
            (flow_id, name, node_type, prompt_text, prompt_audio, prompt_lang,
             transfer_number, gather_timeout_seconds, max_retries)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
        """,
        (flow_id, name, node_type, prompt_text, prompt_audio, "bn",
         transfer_number, timeout, retries),
    )


def _link_demo_node(node_id: int, digit: str, next_id: int, label: str) -> None:
    db.execute(
        """
        INSERT INTO flow_transitions (node_id, digit, next_node_id, label)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (node_id, digit) DO UPDATE
            SET next_node_id = EXCLUDED.next_node_id, label = EXCLUDED.label
        """,
        (node_id, digit, next_id, label),
    )


@customer_bp.route("/flows/demo/solar-bangla", methods=["POST"])
@require_login
@require_role(["admin"])
def flow_demo_solar_bangla():
    tenant_id = g.tenant_id
    
    # Check limit first
    plan = db.query_one(
        "SELECT p.max_flows FROM tenants t JOIN plans p ON t.plan_id = p.id WHERE t.id = %s",
        (tenant_id,)
    )
    max_flows = plan["max_flows"] if plan else 1

    current_flows_count = db.query_one(
        "SELECT COUNT(*) FROM flows WHERE tenant_id = %s",
        (tenant_id,)
    )
    current_flows_count = int(current_flows_count["count"]) if current_flows_count else 0

    if current_flows_count >= max_flows:
        flash(f"Your plan limits you to {max_flows} voice flows. Please upgrade your plan.", "error")
        return redirect(url_for("customer.flows_list"))

    existing = db.query_one("SELECT id FROM flows WHERE name = %s AND tenant_id = %s", ("ডেমো সৌর প্যানেল ব্যবসা IVR", tenant_id))
    if existing:
        flash("Solar panel demo flow already exists. You can edit it now.", "success")
        return redirect(url_for("customer.flow_edit", flow_id=existing["id"]))

    audio_errors: list[str] = []
    flow_id = db.insert_returning_id(
        "INSERT INTO flows (name, description, tenant_id) VALUES (%s, %s, %s) RETURNING id",
        (
            "ডেমো সৌর প্যানেল ব্যবসা IVR",
            "বাংলা সোলার প্যানেল ব্যবসার নমুনা IVR: নতুন কোটেশন, প্যাকেজ, সার্ভিস, EMI এবং প্রতিনিধি।",
            tenant_id,
        ),
    )

    main = _add_demo_node(
        flow_id, "মূল মেনু", "menu",
        "সোলার স্মার্ট বাংলাদেশে আপনাকে স্বাগতম। নতুন সোলার প্যানেল কোটেশনের জন্য ১ চাপুন। "
        "বাসা ও ব্যবসার প্যাকেজ জানতে ২ চাপুন। ইনস্টলেশন বা সার্ভিস সাপোর্টের জন্য ৩ চাপুন। "
        "ই এম আই এবং পেমেন্ট তথ্য জানতে ৪ চাপুন। প্রতিনিধির সাথে কথা বলতে ০ চাপুন।",
        tenant_id, audio_errors=audio_errors,
    )
    quote = _add_demo_node(
        flow_id, "নতুন কোটেশন", "menu",
        "নতুন কোটেশনের জন্য ধন্যবাদ। বাসার সোলার সিস্টেমের জন্য ১ চাপুন। দোকান বা অফিসের জন্য ২ চাপুন। "
        "সেচ পাম্প বা কৃষি ব্যবহারের জন্য ৩ চাপুন। মূল মেনুতে ফিরতে ৯ চাপুন।",
        tenant_id, audio_errors=audio_errors,
    )
    packages = _add_demo_node(
        flow_id, "প্যাকেজ তথ্য", "menu",
        "আমাদের জনপ্রিয় প্যাকেজগুলো শুনুন। এক কিলোওয়াট হোম প্যাকেজ জানতে ১ চাপুন। "
        "তিন কিলোওয়াট ব্যবসা প্যাকেজ জানতে ২ চাপুন। ব্যাটারি ব্যাকআপ প্যাকেজ জানতে ৩ চাপুন। "
        "মূল মেনুতে ফিরতে ৯ চাপুন।",
        tenant_id, audio_errors=audio_errors,
    )
    support = _add_demo_node(
        flow_id, "সার্ভিস সাপোর্ট", "menu",
        "সার্ভিস সাপোর্ট বিভাগে স্বাগতম। ইনভার্টার সমস্যার জন্য ১ চাপুন। ব্যাটারি সমস্যার জন্য ২ চাপুন। "
        "প্যানেল পরিষ্কার বা মেইনটেন্যান্সের জন্য ৩ চাপুন। প্রতিনিধির সাথে কথা বলতে ০ চাপুন। মূল মেনুতে ফিরতে ৯ চাপুন।",
        tenant_id, audio_errors=audio_errors,
    )
    emi = _add_demo_node(
        flow_id, "EMI ও পেমেন্ট", "message",
        "আমাদের সোলার প্যাকেজে সহজ মাসিক কিস্তি সুবিধা আছে। সাধারণত ত্রিশ শতাংশ ডাউন পেমেন্ট দিয়ে কিস্তি শুরু করা যায়। "
        "বিস্তারিত জানতে আমাদের প্রতিনিধি আপনার সাথে যোগাযোগ করবে। ধন্যবাদ।",
        tenant_id, audio_errors=audio_errors,
    )
    agent = _add_demo_node(
        flow_id, "প্রতিনিধির কাছে ট্রান্সফার", "transfer",
        "আপনাকে একজন প্রতিনিধির সাথে সংযুক্ত করা হচ্ছে। অনুগ্রহ করে লাইনে থাকুন।",
        tenant_id, transfer_number="", audio_errors=audio_errors,
    )
    home_quote = _add_demo_node(
        flow_id, "বাসার কোটেশন", "message",
        "বাসার জন্য আমরা আপনার মাসিক বিদ্যুৎ বিল, ছাদের জায়গা এবং ব্যাকআপ প্রয়োজন দেখে কোটেশন দিই। "
        "আমাদের প্রতিনিধি শীঘ্রই আপনার সাথে যোগাযোগ করবে। ধন্যবাদ।",
        tenant_id, audio_errors=audio_errors,
    )
    business_quote = _add_demo_node(
        flow_id, "ব্যবসার কোটেশন", "message",
        "দোকান, অফিস বা কারখানার জন্য লোড ক্যালকুলেশন করে অন-গ্রিড অথবা হাইব্রিড সোলার সিস্টেম দেওয়া হয়। "
        "বিস্তারিত আলোচনার জন্য প্রতিনিধি আপনার সাথে যোগাযোগ করবে।",
        tenant_id, audio_errors=audio_errors,
    )
    pump_quote = _add_demo_node(
        flow_id, "সেচ পাম্প কোটেশন", "message",
        "সেচ পাম্পের জন্য পাম্পের হর্স পাওয়ার, দৈনিক চালানোর সময় এবং জমির দূরত্ব অনুযায়ী সিস্টেম ডিজাইন করা হয়। "
        "আমাদের টিম আপনাকে বিস্তারিত জানাবে।",
        tenant_id, audio_errors=audio_errors,
    )
    home_pkg = _add_demo_node(
        flow_id, "১ কিলোওয়াট হোম প্যাকেজ", "message",
        "এক কিলোওয়াট হোম প্যাকেজে প্যানেল, ইনভার্টার, স্ট্রাকচার এবং ইনস্টলেশন অন্তর্ভুক্ত থাকে। "
        "দাম জায়গা ও ব্র্যান্ড অনুযায়ী পরিবর্তন হতে পারে।",
        tenant_id, audio_errors=audio_errors,
    )
    business_pkg = _add_demo_node(
        flow_id, "৩ কিলোওয়াট ব্যবসা প্যাকেজ", "message",
        "তিন কিলোওয়াট ব্যবসা প্যাকেজ দোকান, অফিস এবং ছোট প্রতিষ্ঠানের জন্য উপযোগী। "
        "লোড অনুযায়ী কাস্টম ডিজাইন করা যায়।",
        tenant_id, audio_errors=audio_errors,
    )
    backup_pkg = _add_demo_node(
        flow_id, "ব্যাটারি ব্যাকআপ প্যাকেজ", "message",
        "ব্যাটারি ব্যাকআপ প্যাকেজে হাইব্রিড ইনভার্টার এবং লিথিয়াম অথবা টিউবুলার ব্যাটারি অপশন আছে। "
        "ব্যাকআপ সময় আপনার লোডের উপর নির্ভর করবে।",
        tenant_id, audio_errors=audio_errors,
    )
    inverter_support = _add_demo_node(
        flow_id, "ইনভার্টার সাপোর্ট", "message",
        "ইনভার্টারে অ্যালার্ম বা এরর দেখালে মেইন সুইচ বন্ধ করে পাঁচ মিনিট পরে চালু করুন। সমস্যা থাকলে সার্ভিস টিম যোগাযোগ করবে।",
        tenant_id, audio_errors=audio_errors,
    )
    battery_support = _add_demo_node(
        flow_id, "ব্যাটারি সাপোর্ট", "message",
        "ব্যাটারির ব্যাকআপ কানেকশন, চার্জিং এবং লোড পরীক্ষা করা প্রয়োজন। আমাদের সার্ভিস টিম আপনাকে সাহায্য করবে।",
        tenant_id, audio_errors=audio_errors,
    )
    cleaning_support = _add_demo_node(
        flow_id, "প্যানেল মেইনটেন্যান্স", "message",
        "সোলার প্যানেল নিয়মিত পরিষ্কার রাখলে উৎপাদন ভালো থাকে। ধুলাবালি বেশি হলে মাসে অন্তত একবার পরিষ্কার করুন।",
        tenant_id, audio_errors=audio_errors,
    )

    _link_demo_node(main, "1", quote, "নতুন কোটেশন")
    _link_demo_node(main, "2", packages, "প্যাকেজ")
    _link_demo_node(main, "3", support, "সাপোর্ট")
    _link_demo_node(main, "4", emi, "EMI")
    _link_demo_node(main, "0", agent, "প্রতিনিধি")
    _link_demo_node(quote, "1", home_quote, "বাসা")
    _link_demo_node(quote, "2", business_quote, "ব্যবসা")
    _link_demo_node(quote, "3", pump_quote, "সেচ পাম্প")
    _link_demo_node(quote, "9", main, "মূল মেনু")
    _link_demo_node(packages, "1", home_pkg, "১ কিলোওয়াট")
    _link_demo_node(packages, "2", business_pkg, "৩ কিলোওয়াট")
    _link_demo_node(packages, "3", backup_pkg, "ব্যাকআপ")
    _link_demo_node(packages, "9", main, "মূল মেনু")
    _link_demo_node(support, "1", inverter_support, "ইনভার্টার")
    _link_demo_node(support, "2", battery_support, "ব্যাটারি")
    _link_demo_node(support, "3", cleaning_support, "মেইনটেন্যান্স")
    _link_demo_node(support, "0", agent, "প্রতিনিধি")
    _link_demo_node(support, "9", main, "মূল মেনু")
    
    db.execute("UPDATE flows SET root_node_id = %s WHERE id = %s AND tenant_id = %s", (main, flow_id, tenant_id))

    if audio_errors:
        flash("Demo flow created, but some prompt audio could not be generated. You can edit/upload audio per node.", "error")
    else:
        flash("Bangla solar panel demo IVR created with ready-to-play prompt audio.", "success")
    return redirect(url_for("customer.flow_edit", flow_id=flow_id))


@customer_bp.route("/flows/<int:flow_id>/delete", methods=["POST"])
@require_login
@require_role(["admin"])
def flow_delete(flow_id: int):
    tenant_id = g.tenant_id
    flow = db.query_one("SELECT name FROM flows WHERE id = %s AND tenant_id = %s", (flow_id, tenant_id))
    if not flow:
        flash("Flow not found or access denied.", "error")
        return redirect(url_for("customer.flows_list"))
        
    db.execute("UPDATE calls SET flow_id = NULL WHERE flow_id = %s AND tenant_id = %s", (flow_id, tenant_id))
    
    # Clean up order confirmation flow selection if this flow was active
    tenant = get_tenant_by_id(tenant_id)
    settings = tenant.get("settings") or {}
    if isinstance(settings, str):
        try:
            settings = json.loads(settings)
        except Exception:
            settings = {}
    if str(settings.get("order_call_flow_id")) == str(flow_id):
        settings.pop("order_call_flow_id", None)
        update_tenant_settings(tenant_id, settings)

    db.execute("DELETE FROM flows WHERE id = %s AND tenant_id = %s", (flow_id, tenant_id))
    flash(f"Flow deleted: {flow['name']}", "success")
    return redirect(url_for("customer.flows_list"))


@customer_bp.route("/flows/<int:flow_id>", methods=["GET"])
@require_login
@require_role(["admin", "agent"])
def flow_edit(flow_id: int):
    tenant_id = g.tenant_id
    flow = db.query_one("SELECT * FROM flows WHERE id = %s AND tenant_id = %s", (flow_id, tenant_id))
    if not flow:
        flash("Flow not found or access denied.", "error")
        return redirect(url_for("customer.flows_list"))
        
    nodes = db.query(
        "SELECT fn.* FROM flow_nodes fn JOIN flows f ON f.id = fn.flow_id "
        "WHERE fn.flow_id = %s AND f.tenant_id = %s ORDER BY fn.id",
        (flow_id, tenant_id)
    )
    transitions = db.query(
        """
        SELECT t.*, n.name AS next_name 
          FROM flow_transitions t 
     LEFT JOIN flow_nodes n ON n.id = t.next_node_id 
         WHERE t.node_id IN (SELECT id FROM flow_nodes WHERE flow_id = %s) 
      ORDER BY t.node_id, t.digit
        """,
        (flow_id,),
    )
    by_node = {}
    for t in transitions:
        by_node.setdefault(t["node_id"], []).append(t)
    return render_template("customer/flow_edit.html", flow=flow, nodes=nodes, transitions=by_node)


@customer_bp.route("/flows/<int:flow_id>/nodes", methods=["POST"])
@require_login
@require_role(["admin"])
def flow_add_node(flow_id: int):
    tenant_id = g.tenant_id
    flow = db.query_one("SELECT id, root_node_id FROM flows WHERE id = %s AND tenant_id = %s", (flow_id, tenant_id))
    if not flow:
        flash("Flow not found or access denied.", "error")
        return redirect(url_for("customer.flows_list"))

    try:
        prompt_audio = build_prompt_audio(tenant_id)
        node_id = db.insert_returning_id(
            """
            INSERT INTO flow_nodes (flow_id, name, node_type, prompt_text, prompt_audio, prompt_lang, transfer_number, gather_timeout_seconds, max_retries) 
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) 
            RETURNING id
            """,
            (
                flow_id,
                request.form.get("name", "").strip() or "Node",
                request.form.get("node_type", "menu"),
                request.form.get("prompt_text", "").strip(),
                prompt_audio,
                request.form.get("prompt_lang", "bn").strip() or "bn",
                request.form.get("transfer_number", "").strip(),
                max(1, min(60, int(request.form.get("gather_timeout_seconds", "8") or 8))),
                max(0, min(5, int(request.form.get("max_retries", "2") or 2))),
            ),
        )
        if not flow["root_node_id"]:
            db.execute("UPDATE flows SET root_node_id = %s WHERE id = %s AND tenant_id = %s", (node_id, flow_id, tenant_id))
        flash("Node added.", "success")
    except Exception as exc:
        flash(str(exc), "error")
    return redirect(url_for("customer.flow_edit", flow_id=flow_id))


@customer_bp.route("/flows/<int:flow_id>/nodes/<int:node_id>/delete", methods=["POST"])
@require_login
@require_role(["admin"])
def flow_delete_node(flow_id: int, node_id: int):
    tenant_id = g.tenant_id
    flow = db.query_one("SELECT id, root_node_id FROM flows WHERE id = %s AND tenant_id = %s", (flow_id, tenant_id))
    if not flow:
        flash("Flow not found or access denied.", "error")
        return redirect(url_for("customer.flows_list"))
        
    db.execute("DELETE FROM flow_transitions WHERE node_id = %s OR next_node_id = %s", (node_id, node_id))
    db.execute("DELETE FROM flow_nodes WHERE id = %s AND flow_id = %s", (node_id, flow_id))
    if flow["root_node_id"] == node_id:
        db.execute("UPDATE flows SET root_node_id = NULL WHERE id = %s AND tenant_id = %s", (flow_id, tenant_id))
    flash("Node deleted.", "success")
    return redirect(url_for("customer.flow_edit", flow_id=flow_id))


@customer_bp.route("/flows/<int:flow_id>/nodes/<int:node_id>/update", methods=["POST"])
@require_login
@require_role(["admin"])
def flow_update_node(flow_id: int, node_id: int):
    tenant_id = g.tenant_id
    flow = db.query_one("SELECT id FROM flows WHERE id = %s AND tenant_id = %s", (flow_id, tenant_id))
    if not flow:
        flash("Flow not found or access denied.", "error")
        return redirect(url_for("customer.flows_list"))

    node = db.query_one("SELECT * FROM flow_nodes WHERE id = %s AND flow_id = %s", (node_id, flow_id))
    if not node:
        flash("Node not found.", "error")
        return redirect(url_for("customer.flow_edit", flow_id=flow_id))

    try:
        node_type = request.form.get("node_type", "menu").strip() or "menu"
        if node_type not in {"menu", "message", "transfer", "hangup"}:
            raise ValueError("Invalid node type.")

        prompt_text = request.form.get("prompt_text", "").strip()
        prompt_lang = request.form.get("prompt_lang", "bn").strip() or "bn"
        prompt_audio = node["prompt_audio"] or ""
        upload = request.files.get("prompt_file")
        regenerate = request.form.get("regenerate_audio") == "1"
        clear_audio = request.form.get("clear_audio") == "1"

        if clear_audio:
            prompt_audio = ""
        elif upload and upload.filename:
            prompt_audio = create_uploaded_audio(upload)
        elif regenerate and prompt_text:
            prompt_audio = generate_order_audio(tenant_id, prompt_text, settings_override={"tts_lang": prompt_lang})

        if node_type == "hangup":
            prompt_text = ""
            prompt_audio = ""

        db.execute(
            """
            UPDATE flow_nodes
               SET name = %s, node_type = %s, prompt_text = %s, prompt_audio = %s,
                   prompt_lang = %s, transfer_number = %s,
                   gather_timeout_seconds = %s, max_retries = %s
             WHERE id = %s AND flow_id = %s
            """,
            (
                request.form.get("name", "").strip() or "Node",
                node_type,
                prompt_text,
                prompt_audio,
                prompt_lang,
                request.form.get("transfer_number", "").strip(),
                max(1, min(60, int(request.form.get("gather_timeout_seconds", "8") or 8))),
                max(0, min(5, int(request.form.get("max_retries", "2") or 2))),
                node_id,
                flow_id,
            ),
        )
        flash("Node updated.", "success")
    except Exception as exc:
        flash(str(exc), "error")
    return redirect(url_for("customer.flow_edit", flow_id=flow_id))


@customer_bp.route("/flows/<int:flow_id>/set-root", methods=["POST"])
@require_login
@require_role(["admin"])
def flow_set_root(flow_id: int):
    tenant_id = g.tenant_id
    flow = db.query_one("SELECT id FROM flows WHERE id = %s AND tenant_id = %s", (flow_id, tenant_id))
    if not flow:
        flash("Flow not found or access denied.", "error")
        return redirect(url_for("customer.flows_list"))

    node_id = int(request.form.get("root_node_id"))
    node = db.query_one("SELECT id FROM flow_nodes WHERE id = %s AND flow_id = %s", (node_id, flow_id))
    if not node:
        flash("Node not found in this flow.", "error")
    else:
        db.execute("UPDATE flows SET root_node_id = %s WHERE id = %s AND tenant_id = %s", (node_id, flow_id, tenant_id))
        flash("Start node updated.", "success")
    return redirect(url_for("customer.flow_edit", flow_id=flow_id))


@customer_bp.route("/flows/<int:flow_id>/transitions", methods=["POST"])
@require_login
@require_role(["admin"])
def flow_add_transition(flow_id: int):
    tenant_id = g.tenant_id
    flow = db.query_one("SELECT id FROM flows WHERE id = %s AND tenant_id = %s", (flow_id, tenant_id))
    if not flow:
        flash("Flow not found or access denied.", "error")
        return redirect(url_for("customer.flows_list"))

    try:
        node_id = int(request.form.get("node_id"))
        next_node_id = int(request.form.get("next_node_id"))
        digit = request.form.get("digit", "").strip()
        if digit not in [str(d) for d in range(10)] + ["*", "#"]:
            raise ValueError("Digit must be 0-9, * or #.")

        # Ensure both nodes belong to this flow
        node = db.query_one("SELECT id FROM flow_nodes WHERE id = %s AND flow_id = %s", (node_id, flow_id))
        next_node = db.query_one("SELECT id FROM flow_nodes WHERE id = %s AND flow_id = %s", (next_node_id, flow_id))
        if not node or not next_node:
            raise ValueError("Invalid node selection.")

        db.execute(
            """
            INSERT INTO flow_transitions (node_id, digit, next_node_id, label)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (node_id, digit) DO UPDATE
                SET next_node_id = EXCLUDED.next_node_id, label = EXCLUDED.label
            """,
            (node_id, digit, next_node_id, request.form.get("label", "").strip()),
        )
        flash(f"Keypress {digit} mapped successfully.", "success")
    except Exception as exc:
        flash(str(exc), "error")
    return redirect(url_for("customer.flow_edit", flow_id=flow_id))


@customer_bp.route("/flows/<int:flow_id>/transitions/<int:transition_id>/delete", methods=["POST"])
@require_login
@require_role(["admin"])
def flow_delete_transition(flow_id: int, transition_id: int):
    tenant_id = g.tenant_id
    flow = db.query_one("SELECT id FROM flows WHERE id = %s AND tenant_id = %s", (flow_id, tenant_id))
    if not flow:
        flash("Flow not found or access denied.", "error")
        return redirect(url_for("customer.flows_list"))

    # Verify transition belongs to this flow
    trans = db.query_one(
        """
        SELECT t.id FROM flow_transitions t
          JOIN flow_nodes n ON n.id = t.node_id
         WHERE t.id = %s AND n.flow_id = %s
        """,
        (transition_id, flow_id)
    )
    if not trans:
        flash("Transition not found or access denied.", "error")
    else:
        db.execute("DELETE FROM flow_transitions WHERE id = %s", (transition_id,))
        flash("Transition removed.", "success")
    return redirect(url_for("customer.flow_edit", flow_id=flow_id))


def validate_flow_graph(flow_id: int, tenant_id: int) -> dict:
    """Validate IVR flow integrity: root ownership, missing targets, unreachable nodes, and dead-end menus."""
    flow = db.query_one("SELECT id, root_node_id FROM flows WHERE id = %s AND tenant_id = %s", (flow_id, tenant_id))
    if not flow:
        return {"valid": False, "errors": ["Flow not found or access denied"], "warnings": []}

    nodes = db.query("SELECT id, name, node_type FROM flow_nodes WHERE flow_id = %s", (flow_id,))
    node_map = {n["id"]: n for n in nodes}
    transitions = db.query(
        "SELECT t.node_id, t.digit, t.next_node_id FROM flow_transitions t "
        "JOIN flow_nodes n ON n.id = t.node_id WHERE n.flow_id = %s",
        (flow_id,),
    )

    errors: list[str] = []
    warnings: list[str] = []

    root_id = flow.get("root_node_id")
    if not root_id or root_id not in node_map:
        errors.append("Flow has no valid start (root) node.")

    adj: dict[int, list[int]] = {}
    for t in transitions:
        if t["next_node_id"] not in node_map:
            errors.append(f"Transition digit '{t['digit']}' on node {t['node_id']} points to missing node {t['next_node_id']}.")
        else:
            adj.setdefault(t["node_id"], []).append(t["next_node_id"])

    for nid, n in node_map.items():
        if n["node_type"] == "menu" and not adj.get(nid):
            warnings.append(f"Menu node '{n['name']}' has no outgoing DTMF transitions.")

    if root_id and root_id in node_map:
        visited: set[int] = set()
        stack = [root_id]
        while stack:
            curr = stack.pop()
            if curr in visited:
                continue
            visited.add(curr)
            for nxt in adj.get(curr, []):
                if nxt not in visited:
                    stack.append(nxt)
        unreachable = [n["name"] for nid, n in node_map.items() if nid not in visited]
        if unreachable:
            warnings.append(f"Unreachable nodes: {', '.join(unreachable)}")

    return {"valid": len(errors) == 0, "errors": errors, "warnings": warnings}


@customer_bp.route("/flows/<int:flow_id>/validate", methods=["GET"])
@require_login
@require_role(["admin", "agent"])
def flow_validate(flow_id: int):
    result = validate_flow_graph(flow_id, g.tenant_id)
    return jsonify(result), (200 if result["valid"] else 400)
