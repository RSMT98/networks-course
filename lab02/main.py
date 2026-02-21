import os
from flask import Flask, request, jsonify, abort, send_from_directory
from werkzeug.exceptions import BadRequest
from werkzeug.utils import secure_filename

UPLOAD_FOLDER = "icons"
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
products = {
    1: {"id": 1, "name": "Ноутбук", "description": "Оч крутой ноутбук"},
    2: {"id": 2, "name": "Мышь", "description": "Оч крутая мышь"},
}
next_product_id = 3

@app.errorhandler(BadRequest)
def handle_bad_request(e):
    return jsonify({"error": "Invalid JSON"}), 400

@app.errorhandler(404)
def handle_not_found(e):
    return jsonify({"error": "The requested resource was not found"}), 404

@app.errorhandler(405)
def handle_method_not_allowed(e):
    return jsonify({"error": "Method not allowed for the requested URL"}), 405

def allowed_icon(filename: str | None) -> bool:
    if not filename:
        return False
    return ("." in filename) and (filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS)

def remove_file_if_exists(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass

@app.route('/product', methods=['POST'])
def add_product():
    data = request.get_json()
    if not data or 'name' not in data or 'description' not in data:
        return jsonify({"error": "Missing required fields: 'name' and 'description'"}), 400
    if not isinstance(data['name'], str) or not isinstance(data['description'], str):
        return jsonify({"error": "Fields 'name' and 'description' must be strings"}), 400

    global next_product_id
    new_product = {
        "id": next_product_id,
        "name": data['name'],
        "description": data['description'],
    }
    products[next_product_id] = new_product
    next_product_id += 1

    return jsonify(new_product), 201

@app.route('/product/<int:product_id>', methods=['GET'])
def get_product(product_id):
    product = products.get(product_id)
    if not product:
        abort(404)
    return jsonify(product)

@app.route('/product/<int:product_id>', methods=['PUT'])
def update_product(product_id):
    if product_id not in products:
        abort(404)

    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body cannot be empty"}), 400
    if "id" in data and data['id'] != product_id:
        return jsonify({"error": "Field 'id' cannot be modified"}), 400
    if "icon" in data:
        return jsonify({"error": "Field 'icon' cannot be modified here"}), 400
    data.pop('id', None)
    data.pop("icon", None)

    if 'name' in data:
        if not isinstance(data['name'], str):
            return jsonify({"error": "'name' must be a string"}), 400
        products[product_id]['name'] = data['name']
    if 'description' in data:
        if not isinstance(data['description'], str):
            return jsonify({"error": "'description' must be a string"}), 400
        products[product_id]['description'] = data['description']

    return jsonify(products[product_id])

@app.route('/product/<int:product_id>', methods=['DELETE'])
def delete_product(product_id):
    if product_id not in products:
        abort(404)
        
    icon = products[product_id].get("icon")
    if icon:
        remove_file_if_exists(os.path.join(app.config["UPLOAD_FOLDER"], icon))

    deleted = products.pop(product_id)
    return jsonify(deleted)

@app.route('/products', methods=['GET'])
def get_all_products():
    return jsonify(list(products.values()))

@app.route("/product/<int:product_id>/image", methods=["POST"])
def upload_image(product_id):
    if product_id not in products:
        abort(404)
    if "icon" not in request.files:
        return jsonify({"error": "No file part 'icon' in the request"}), 400

    icon = request.files["icon"]
    if not icon.filename:
        return jsonify({"error": "No selected icon"}), 400
    if not allowed_icon(icon.filename):
        return (
            jsonify(
                {
                    "error": (
                        "Unsupported icon type. Allowed: "
                        f"{', '.join(sorted(ALLOWED_EXTENSIONS))}"
                    )
                }
            ),
            415,
        )
    
    old_icon = products[product_id].get("icon")
    if old_icon:
        remove_file_if_exists(os.path.join(app.config["UPLOAD_FOLDER"], old_icon))

    ext = icon.filename.rsplit(".", 1)[1].lower()
    filename = secure_filename(f"{product_id}.{ext}")
    filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    icon.save(filepath)

    products[product_id]["icon"] = filename
    return jsonify(products[product_id]), 201

@app.route("/product/<int:product_id>/image", methods=["GET"])
def get_image(product_id):
    product = products.get(product_id)
    if not product:
        abort(404)
    icon = product.get('icon')
    if not icon:
        return jsonify({"error": "No icon uploaded for this product"}), 404
    return send_from_directory(app.config["UPLOAD_FOLDER"], icon, as_attachment=False)

if __name__ == '__main__':
    app.run(debug=True, port=5000)
