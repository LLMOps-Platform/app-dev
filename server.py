from flask import Flask, request, redirect, url_for, Response, render_template
import os
import threading
from werkzeug.utils import secure_filename
import gradio as gr
import subprocess
import socket
import re, time
import requests
import zipfile
import json
import datetime
app = Flask(__name__)
UPLOAD_FOLDER = "models"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Home page: links to list models and model upload form
@app.route("/")
def index():
    return render_template("index.html")

# Endpoint to handle model uploads
@app.route("/upload", methods=["POST"])
def upload_model():
    required_files = ["model_definition", "model_weights", "requirements"]
    for file_key in required_files:
        if file_key not in request.files:
            return f"No file provided for {file_key}", 400

    # Get model_name from form data
    model_name = request.form.get("model_name", "").strip()
    if not model_name:
        return "Model name is required", 400

    model_def_file = request.files["model_definition"]
    weights_file = request.files["model_weights"]
    req_file = request.files["requirements"]

    if model_def_file.filename == "" or weights_file.filename == "" or req_file.filename == "":
        return "One or more files were not selected", 400

    # Use the provided model_name (force sanitized) as the model identifier.
    model_folder = os.path.join(UPLOAD_FOLDER, secure_filename(model_name))
    os.makedirs(model_folder, exist_ok=True)

    # Force saving the model definition as app.py and requirements as requirements.txt.
    model_def_path = os.path.join(model_folder, "app.py")
    weights_path = os.path.join(model_folder, secure_filename(weights_file.filename))
    req_path = os.path.join(model_folder, "requirements.txt")

    # Create descriptor.json with model metadata
    import json
    from datetime import datetime

    # Read requirements from requirements.txt
    # Read requirements directly from the uploaded file object
    requirements = req_file.read().decode('utf-8').splitlines()
    requirements_list = [req.strip() for req in requirements if req.strip()]
    # Remove any version specifiers from requirements
    requirements_list = [re.sub(r"==.*", "", req) for req in requirements_list]
    descriptor = {
        "model_name": model_name,
        "files": {
            "model_definition": os.path.join(model_folder, "app.py"),
            "model_weights": os.path.join(model_folder, secure_filename(weights_file.filename)),
        },
        "requirements": requirements_list,
        "version": request.form.get("version", "1.0"),
        "interface_type": request.form.get("interface_type", "gradio"),  # Default to gradio if not specified
    }
    # Save all files
    model_def_file.save(model_def_path)
    weights_file.save(weights_path)
    req_file.save(req_path)

    # Create deployment artifacts directory if it doesn't exist
    artifacts_dir = os.path.join(UPLOAD_FOLDER, model_name)
    os.makedirs(artifacts_dir, exist_ok=True)

    # Create timestamp for unique filenames
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_filename = f"{secure_filename(model_name)}_{timestamp}.zip"
    zip_path = os.path.join(artifacts_dir, zip_filename)
    descriptor_filename = "descriptor.json"
    descriptor_path = os.path.join(artifacts_dir, descriptor_filename)

    # Create zip file with model artifacts (excluding descriptor)
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        zipf.write(model_def_path, arcname=os.path.basename(model_def_path))
        zipf.write(weights_path, arcname=os.path.basename(weights_path))
        zipf.write(req_path, arcname=os.path.basename(req_path))

    port_no = deploy_model(model_name, zip_path, descriptor)
    if port_no is None:
        return "Model deployment failed", 500

    descriptor["port"] = port_no
    with open(descriptor_path, 'w') as f:
        json.dump(descriptor, f, indent=4)

    return redirect(url_for("list_models"))


def deploy_model(model_name, zip_path, descriptor):
    """
    Simulates the lifecycle manager to provision a container and deploy the model
    """
    print(f"Deploying model {model_name}...")

    # Create deployed_models directory if it doesn't exist
    deployed_dir = os.path.join("deployed_models")
    os.makedirs(deployed_dir, exist_ok=True)

    # Find an available port
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(('', 0))
    port_no = s.getsockname()[1]
    s.close()

    # Create model-specific deployment directory with port in the name
    model_deploy_dir = os.path.join(deployed_dir, f"{port_no}")
    os.makedirs(model_deploy_dir, exist_ok=True)
    # Extract the zip contents to the model's deployment directory
    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
        zip_ref.extractall(model_deploy_dir)

    deployed_descriptor_path = os.path.join(model_deploy_dir, "descriptor.json")

    # Use the descriptor parameter directly instead of trying to read from an undefined path
    descriptor_data = descriptor.copy()
    # Update descriptor with deployment info
    descriptor_data["deployed_at"] = datetime.datetime.now().isoformat()
    descriptor_data["status"] = "deployed"
    descriptor_data["port"] = port_no

    # Save updated descriptor in deployed directory
    with open(deployed_descriptor_path, 'w') as dest_file:
        json.dump(descriptor_data, dest_file, indent=4)

    # create virtual environment based on dependencies in descriptor.json
    venv_path = os.path.join(model_deploy_dir, "venv")
    subprocess.run(["python", "-m", "venv", venv_path])
    # Install pip and dependencies inside the venv
    pip_command = os.path.join(venv_path, "bin" if os.name != "nt" else "Scripts", "pip")
    subprocess.run([pip_command, "install", "--upgrade", "pip"])

    # Install dependencies from descriptor.json
    for dependency in descriptor_data["requirements"]:
        subprocess.run([pip_command, "install", dependency])

    app.logger.info(f"Model {model_name} deployed on port {port_no}")
    return port_no

# Endpoint to list available models
@app.route("/models", methods=["GET"])
def list_models():
    models = os.listdir(UPLOAD_FOLDER)
    return render_template("models.html", models=models)

gradio_servers = {}


@app.route("/model/<model_name>", methods=["GET"])
def model_specific(model_name):
    if model_name not in os.listdir(UPLOAD_FOLDER):
        return "Model not found", 404

    # Load model descriptor
    descriptor_path = os.path.join(UPLOAD_FOLDER, model_name, "descriptor.json")

    if not os.path.exists(descriptor_path):
        return f"Descriptor file for model {model_name} not found", 404

    with open(descriptor_path, 'r') as f:
        descriptor = json.load(f)

    # Check descriptor for interface type
    interface_type = descriptor.get("interface_type", "gradio")
    port_no = descriptor.get("port", None)

    if port_no is None:
        return f"Model {model_name} is not properly deployed (no port defined)", 400

    if interface_type != "gradio":
        return f"Interface type {interface_type} is not supported", 400

    # Check if the model is already running
    if model_name in gradio_servers:
        # Instead of redirecting, just use the existing port
        port = gradio_servers[model_name]["port"]
        return render_template("model_interface.html", model_name=model_name, port=port)

    # If this model's server hasn't been started yet, create and launch it
    model_folder = os.path.join("deployed_models", str(port_no))

    if not os.path.exists(model_folder):
        return f"Deployment folder for model {model_name} not found", 404

    model_file = os.path.join(model_folder, "app.py")
    print(f"Launching model server for {model_name}...")
    print(f"Model file: {model_file}")

    env = os.environ.copy()
    env["GRADIO_SERVER_PORT"] = str(port_no)
    env["GRADIO_ROOT_PATH"] = f"/model/{model_name}"

    try:
        process = subprocess.Popen(
            ["python", "app.py"],
            cwd=model_folder,
            env=env,
        )
        gradio_servers[model_name] = {"process": process, "port": port_no}
    except Exception as e:
        return f"Failed to start model server: {str(e)}", 500

    # Return template with iframe to Gradio interface
    return render_template("model_interface.html", model_name=model_name, port=port_no)

# Endpoint to handle model API requests does not get called in frontend
@app.route("/model/<model_name>/<path:subpath>", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
def proxy_model_api(model_name, subpath):
    # Ensure the model's Gradio container is running
    if model_name not in gradio_servers:
        return f"Model {model_name} is not running", 404

    port = gradio_servers[model_name]["port"]

    # Build the target URL (note the leading '/' for subpath) and include query parameters
    target_url = f"http://localhost:{port}/{subpath}"
    params = dict(request.args)
    if "session_hash" not in params:
        params["session_hash"] = "1234"
    print(f"Proxying request for {model_name} to {target_url} with params {params}")

    # Forward the original request (method, headers, data, etc)
    resp = requests.request(
        method=request.method,
        url=target_url,
        params=params,
        headers={key: value for key, value in request.headers if key != "Host"},
        data=request.get_data(),
        cookies=request.cookies,
        allow_redirects=False
    )
    excluded_headers = ["content-encoding", "content-length", "transfer-encoding", "connection"]
    headers = [(name, value) for name, value in resp.raw.headers.items() if name.lower() not in excluded_headers]

    return Response(resp.content, resp.status_code, headers)

# Add new routes for API Doc and Instances Running under a specific model
@app.route("/model/<model_name>/api_doc")
def api_doc_model(model_name):
    if model_name not in os.listdir(UPLOAD_FOLDER):
        return "Model not found", 404
    return render_template("api_doc.html", model_name=model_name)

@app.route("/model/<model_name>/instances")
def instances_model(model_name):
    count = 1 if model_name in gradio_servers else 0
    return render_template("instances.html", model_name=model_name, count=count)

if __name__ == "__main__":
    app.run(debug=True, port=5000, use_reloader=False)