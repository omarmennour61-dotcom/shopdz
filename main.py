"""
DZ Shop Omar — Boutique en ligne
===================================
Application Flask: vitrine e-commerce + panneau d'administration
(gestion des catégories, produits, commandes).
"""
import os, sys, uuid
from datetime import datetime
from functools import wraps
from flask import (Flask, render_template, request, redirect, url_for, session,
                    flash, jsonify, send_from_directory)
from werkzeug.utils import secure_filename

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from database import init_db, get_connection, check_admin_login, get_store_settings, update_store_settings

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "static", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
ALLOWED_EXT = {"png", "jpg", "jpeg", "webp", "gif"}

app = Flask(__name__)
app.secret_key = "dzshop-omar-secret-key-2026"
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # 8 MB uploads


def admin_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not session.get("admin"):
            return redirect(url_for("admin_login"))
        return f(*a, **kw)
    return wrapper


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXT


@app.context_processor
def inject_globals():
    settings = get_store_settings()
    cart = session.get("cart", {})
    cart_count = sum(cart.values()) if cart else 0
    conn = get_connection()
    categories = conn.execute("SELECT * FROM categories ORDER BY sort_order, name").fetchall()
    conn.close()
    return dict(store=settings, cart_count=cart_count, nav_categories=categories,
                now_year=datetime.now().year)


# ============================================================ STOREFRONT
@app.route("/")
def home():
    conn = get_connection()
    featured = conn.execute("""SELECT p.*, c.name cat_name, c.slug cat_slug FROM products p
        LEFT JOIN categories c ON c.id=p.category_id
        WHERE p.is_active=1 AND p.featured=1 ORDER BY p.id DESC LIMIT 8""").fetchall()
    latest = conn.execute("""SELECT p.*, c.name cat_name, c.slug cat_slug FROM products p
        LEFT JOIN categories c ON c.id=p.category_id
        WHERE p.is_active=1 ORDER BY p.id DESC LIMIT 12""").fetchall()
    categories = conn.execute("SELECT * FROM categories ORDER BY sort_order, name").fetchall()
    conn.close()
    return render_template("home.html", featured=featured, latest=latest, categories=categories)


@app.route("/categorie/<slug>")
def category_view(slug):
    conn = get_connection()
    cat = conn.execute("SELECT * FROM categories WHERE slug=?", (slug,)).fetchone()
    if not cat:
        conn.close()
        return "Catégorie introuvable", 404
    q = request.args.get("q", "").strip()
    sort = request.args.get("sort", "recent")
    query = """SELECT p.*, c.name cat_name, c.slug cat_slug FROM products p
               LEFT JOIN categories c ON c.id=p.category_id
               WHERE p.is_active=1 AND p.category_id=?"""
    params = [cat["id"]]
    if q:
        query += " AND p.name LIKE ?"
        params.append(f"%{q}%")
    order = {"price_asc": " ORDER BY p.price ASC", "price_desc": " ORDER BY p.price DESC",
              "recent": " ORDER BY p.id DESC"}.get(sort, " ORDER BY p.id DESC")
    query += order
    products = conn.execute(query, params).fetchall()
    conn.close()
    return render_template("category.html", category=cat, products=products, q=q, sort=sort)


@app.route("/recherche")
def search():
    q = request.args.get("q", "").strip()
    conn = get_connection()
    products = []
    if q:
        products = conn.execute("""SELECT p.*, c.name cat_name, c.slug cat_slug FROM products p
            LEFT JOIN categories c ON c.id=p.category_id
            WHERE p.is_active=1 AND p.name LIKE ? ORDER BY p.id DESC""", (f"%{q}%",)).fetchall()
    conn.close()
    return render_template("search.html", products=products, q=q)


@app.route("/produit/<int:pid>")
def product_detail(pid):
    conn = get_connection()
    p = conn.execute("""SELECT p.*, c.name cat_name, c.slug cat_slug FROM products p
        LEFT JOIN categories c ON c.id=p.category_id WHERE p.id=?""", (pid,)).fetchone()
    if not p:
        conn.close()
        return "Produit introuvable", 404
    related = conn.execute("""SELECT * FROM products WHERE category_id=? AND id!=? AND is_active=1
        ORDER BY RANDOM() LIMIT 4""", (p["category_id"], pid)).fetchall()
    conn.close()
    return render_template("product.html", p=p, related=related)


# ------------------------------------------------------------ CART (session-based)
@app.route("/panier")
def cart_view():
    cart = session.get("cart", {})
    conn = get_connection()
    items, total = [], 0
    for pid_str, qty in cart.items():
        p = conn.execute("SELECT * FROM products WHERE id=?", (int(pid_str),)).fetchone()
        if p:
            subtotal = round(p["price"] * qty, 2)
            total += subtotal
            items.append({"product": p, "qty": qty, "subtotal": subtotal})
    conn.close()
    return render_template("cart.html", items=items, total=round(total, 2))


@app.route("/panier/ajouter/<int:pid>", methods=["POST"])
def cart_add(pid):
    qty = int(request.form.get("qty", 1))
    cart = session.get("cart", {})
    key = str(pid)
    cart[key] = cart.get(key, 0) + max(qty, 1)
    session["cart"] = cart
    session.modified = True
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"ok": True, "cart_count": sum(cart.values())})
    flash("Produit ajouté au panier.", "success")
    return redirect(request.referrer or url_for("home"))


@app.route("/panier/maj/<int:pid>", methods=["POST"])
def cart_update(pid):
    qty = int(request.form.get("qty", 1))
    cart = session.get("cart", {})
    key = str(pid)
    if qty <= 0:
        cart.pop(key, None)
    else:
        cart[key] = qty
    session["cart"] = cart
    session.modified = True
    return redirect(url_for("cart_view"))


@app.route("/panier/retirer/<int:pid>")
def cart_remove(pid):
    cart = session.get("cart", {})
    cart.pop(str(pid), None)
    session["cart"] = cart
    session.modified = True
    return redirect(url_for("cart_view"))


@app.route("/commander", methods=["GET", "POST"])
def checkout():
    cart = session.get("cart", {})
    if not cart:
        return redirect(url_for("home"))
    conn = get_connection()
    items, total = [], 0
    for pid_str, qty in cart.items():
        p = conn.execute("SELECT * FROM products WHERE id=?", (int(pid_str),)).fetchone()
        if p:
            subtotal = round(p["price"] * qty, 2)
            total += subtotal
            items.append({"product": p, "qty": qty, "subtotal": subtotal})

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        wilaya = request.form.get("wilaya", "").strip()
        address = request.form.get("address", "").strip()
        notes = request.form.get("notes", "").strip()
        if not name or not phone:
            flash("Nom et téléphone sont obligatoires.", "danger")
            conn.close()
            return render_template("checkout.html", items=items, total=round(total, 2))

        cur = conn.cursor()
        cur.execute("""INSERT INTO orders(customer_name,phone,wilaya,address,total,status,notes,created_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (name, phone, wilaya, address, round(total, 2), "En attente", notes,
                     datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        order_id = cur.lastrowid
        for it in items:
            cur.execute("""INSERT INTO order_items(order_id,product_id,product_name,quantity,unit_price,subtotal)
                           VALUES(?,?,?,?,?,?)""",
                        (order_id, it["product"]["id"], it["product"]["name"], it["qty"],
                         it["product"]["price"], it["subtotal"]))
        conn.commit()
        conn.close()
        session["cart"] = {}
        session.modified = True
        return redirect(url_for("order_confirmation", order_id=order_id))

    conn.close()
    return render_template("checkout.html", items=items, total=round(total, 2))


@app.route("/commande/confirmation/<int:order_id>")
def order_confirmation(order_id):
    conn = get_connection()
    order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    items = conn.execute("SELECT * FROM order_items WHERE order_id=?", (order_id,)).fetchall()
    conn.close()
    if not order:
        return redirect(url_for("home"))
    return render_template("order_confirmation.html", order=order, items=items)


# ============================================================ ADMIN
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        u = request.form.get("username", "")
        p = request.form.get("password", "")
        if check_admin_login(u, p):
            session["admin"] = u
            return redirect(url_for("admin_dashboard"))
        return render_template("admin/login.html", error="Identifiants incorrects.")
    return render_template("admin/login.html", error=None)


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect(url_for("home"))


@app.route("/admin")
@admin_required
def admin_dashboard():
    conn = get_connection()
    n_products = conn.execute("SELECT COUNT(*) c FROM products").fetchone()["c"]
    n_categories = conn.execute("SELECT COUNT(*) c FROM categories").fetchone()["c"]
    n_orders = conn.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"]
    pending_orders = conn.execute("SELECT COUNT(*) c FROM orders WHERE status='En attente'").fetchone()["c"]
    revenue = conn.execute("SELECT COALESCE(SUM(total),0) s FROM orders WHERE status!='Annulée'").fetchone()["s"]
    low_stock = conn.execute("SELECT * FROM products WHERE stock_qty<=5 AND is_active=1 LIMIT 8").fetchall()
    recent_orders = conn.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 8").fetchall()
    conn.close()
    return render_template("admin/dashboard.html", n_products=n_products, n_categories=n_categories,
                          n_orders=n_orders, pending_orders=pending_orders, revenue=revenue,
                          low_stock=low_stock, recent_orders=recent_orders)


# --------- Admin: Products
@app.route("/admin/produits")
@admin_required
def admin_products():
    conn = get_connection()
    products = conn.execute("""SELECT p.*, c.name cat_name FROM products p
        LEFT JOIN categories c ON c.id=p.category_id ORDER BY p.id DESC""").fetchall()
    categories = conn.execute("SELECT * FROM categories ORDER BY name").fetchall()
    conn.close()
    return render_template("admin/products.html", products=products, categories=categories)


@app.route("/admin/produits/save", methods=["POST"])
@admin_required
def admin_products_save():
    f = request.form
    image_path = f.get("existing_image", "")
    file = request.files.get("image_file")
    if file and file.filename and allowed_file(file.filename):
        fname = secure_filename(f"{uuid.uuid4().hex}_{file.filename}")
        file.save(os.path.join(UPLOAD_DIR, fname))
        image_path = "uploads/" + fname

    conn = get_connection()
    if f.get("id"):
        conn.execute("""UPDATE products SET name=?,category_id=?,sku=?,price=?,old_price=?,stock_qty=?,
                       description=?,image=?,featured=?,is_active=? WHERE id=?""",
                    (f["name"], f.get("category_id") or None, f.get("sku",""), float(f.get("price",0)),
                     float(f.get("old_price",0) or 0), int(f.get("stock_qty",0)), f.get("description",""),
                     image_path, 1 if f.get("featured")=="on" else 0, 1 if f.get("is_active")=="on" else 0,
                     f["id"]))
        flash("Produit mis à jour.", "success")
    else:
        conn.execute("""INSERT INTO products(name,category_id,sku,price,old_price,stock_qty,description,
                       image,featured,is_active,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (f["name"], f.get("category_id") or None, f.get("sku",""), float(f.get("price",0)),
                     float(f.get("old_price",0) or 0), int(f.get("stock_qty",0)), f.get("description",""),
                     image_path, 1 if f.get("featured")=="on" else 0, 1 if f.get("is_active")=="on" else 0,
                     datetime.now().isoformat()))
        flash("Produit ajouté avec succès.", "success")
    conn.commit()
    conn.close()
    return redirect(url_for("admin_products"))


@app.route("/admin/produits/supprimer/<int:pid>", methods=["POST"])
@admin_required
def admin_products_delete(pid):
    conn = get_connection()
    conn.execute("DELETE FROM products WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    flash("Produit supprimé.", "success")
    return redirect(url_for("admin_products"))


# --------- Admin: Categories
@app.route("/admin/categories")
@admin_required
def admin_categories():
    conn = get_connection()
    categories = conn.execute("""SELECT c.*, (SELECT COUNT(*) FROM products p WHERE p.category_id=c.id) n_products
        FROM categories c ORDER BY c.sort_order, c.name""").fetchall()
    conn.close()
    return render_template("admin/categories.html", categories=categories)


@app.route("/admin/categories/save", methods=["POST"])
@admin_required
def admin_categories_save():
    f = request.form
    name = f.get("name", "").strip()
    slug = f.get("slug", "").strip().lower().replace(" ", "-")
    icon = f.get("icon", "bi-grid")
    conn = get_connection()
    try:
        if f.get("id"):
            conn.execute("UPDATE categories SET name=?,slug=?,icon=? WHERE id=?", (name, slug, icon, f["id"]))
            flash("Catégorie mise à jour.", "success")
        else:
            conn.execute("INSERT INTO categories(name,slug,icon,sort_order) VALUES(?,?,?,99)", (name, slug, icon))
            flash("Catégorie ajoutée.", "success")
        conn.commit()
    except Exception:
        flash("Erreur: ce nom ou slug existe déjà.", "danger")
    conn.close()
    return redirect(url_for("admin_categories"))


@app.route("/admin/categories/supprimer/<int:cid>", methods=["POST"])
@admin_required
def admin_categories_delete(cid):
    conn = get_connection()
    conn.execute("DELETE FROM categories WHERE id=?", (cid,))
    conn.commit()
    conn.close()
    flash("Catégorie supprimée.", "success")
    return redirect(url_for("admin_categories"))


# --------- Admin: Orders
@app.route("/admin/commandes")
@admin_required
def admin_orders():
    conn = get_connection()
    orders = conn.execute("SELECT * FROM orders ORDER BY id DESC").fetchall()
    conn.close()
    return render_template("admin/orders.html", orders=orders)


@app.route("/admin/commandes/<int:order_id>")
@admin_required
def admin_order_detail(order_id):
    conn = get_connection()
    order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    items = conn.execute("SELECT * FROM order_items WHERE order_id=?", (order_id,)).fetchall()
    conn.close()
    return render_template("admin/order_detail.html", order=order, items=items)


@app.route("/admin/commandes/statut/<int:order_id>", methods=["POST"])
@admin_required
def admin_order_status(order_id):
    status = request.form.get("status")
    conn = get_connection()
    conn.execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))
    conn.commit()
    conn.close()
    flash("Statut de la commande mis à jour.", "success")
    return redirect(url_for("admin_order_detail", order_id=order_id))


# --------- Admin: Settings
@app.route("/admin/parametres", methods=["GET", "POST"])
@admin_required
def admin_settings():
    if request.method == "POST":
        update_store_settings(dict(request.form))
        flash("Paramètres de la boutique enregistrés.", "success")
        return redirect(url_for("admin_settings"))
    return render_template("admin/settings.html", s=get_store_settings())


# --------- PWA files (manifest, service worker) served from root scope
@app.route("/manifest.json")
def manifest():
    return send_from_directory(os.path.join(BASE_DIR, "static"), "manifest.json",
                                mimetype="application/manifest+json")


@app.route("/service-worker.js")
def service_worker():
    return send_from_directory(BASE_DIR, "service-worker.js", mimetype="application/javascript")


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    print("\n" + "=" * 60)
    print("  DZ SHOP OMAR — Serveur démarré")
    print(f"  Boutique : http://127.0.0.1:{port}")
    print(f"  Admin    : http://127.0.0.1:{port}/admin/login  (admin / admin)")
    print("=" * 60 + "\n")
    app.run(host="0.0.0.0", port=port, debug=False)
