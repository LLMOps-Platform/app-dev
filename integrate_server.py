import random
import os
import threading
import subprocess
import socket
import re
import time
import json
import datetime
import tempfile
import shutil
import logging
import zipfile
import requests
from datetime import timezone
from flask import Flask, request, redirect, url_for, Response, render_template
from werkzeug.utils import secure_filename
from kafka import KafkaProducer

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

app = Flask(__name__)

## Configuration
REGISTRY_URL = "http://localhost:5000"      # Registry server URL
SERVER_LIFECYCLE_URL = "http://localhost:5001"  # Lifecycle server URL
KAFKA_BROKER = "10.1.37.28:9092"
KAFKA_TOPIC = "logs"


def init_kafka_producer():
    """Initialise the Kafka producer."""
    try:
        producer = KafkaProducer(
            bootstrap_servers=KAFKA_BROKER,
            value_serializer=lambda v: json.dumps(v).encode("utf-8")
        )
        logger.info("Kafka producer initialized.")
        return producer
    except Exception as e:
        logger.warning(f"Could not initialize Kafka producer: {e}")
        return None


kafka_producer = init_kafka_producer()


def log_message(server: str, log_msg: str):
    """Send a structured log message to Kafka."""
    if kafka_producer is None:
        logger.warning("Kafka producer not available. Skipping Kafka log.")
        return

    message = {
        "server": server,
        "log": log_msg,
        "timestamp": datetime.datetime.now(timezone.utc).isoformat()
    }
    try:
        kafka_producer.send(KAFKA_TOPIC, message)
        kafka_producer.flush()
        logger.info(f"Sent log to Kafka: {message}")
    except Exception as e:
        logger.warning(f"Failed to send log to Kafka: {e}")


def deploy_model(model_id):
    """Deploy the model using the lifecycle endpoint."""
    try:
        payload = {"model_id": model_id}
        response = requests.post(f"{SERVER_LIFECYCLE_URL}/deploy_server", json=payload)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        log_message(model_id, f"Deployment failed: {e}")
        return {"error": str(e)}


def undeploy_model(model_id):
    """Undeploy the model using the lifecycle endpoint."""
    try:
        payload = {"model_id": model_id}
        response = requests.post(f"{SERVER_LIFECYCLE_URL}/undeploy_server", json=payload)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        log_message(model_id, f"Undeployment failed: {e}")
        return {"error": str(e)}


def get_applications():
    """Retrieve all registered applications from the registry."""
    try:
        response = requests.get(f"{REGISTRY_URL}/applications")
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        log_message("Registry", f"Failed to retrieve models: {e}")
        return None


def tag_and_store_release(model_name, web_app_file, inference_app_file):
    """
    Tag the release via the repository endpoint and then store model information.
    Returns the repository response JSON on success, or an error message on failure.
    """
    repository_url = f"{REGISTRY_URL}/tag_release"
    files = {
        "web_app": (secure_filename(web_app_file.filename),
                    web_app_file.stream, web_app_file.mimetype),
        "inference_app": (secure_filename(inference_app_file.filename),
                          inference_app_file.stream, inference_app_file.mimetype)
    }
    data = {"model_name": model_name}

    try:
        repo_response = requests.post(repository_url, data=data, files=files)
        repo_response.raise_for_status()
        release_info = repo_response.json()
    except requests.exceptions.RequestException as e:
        log_message(model_name, f"Repository tagging failed: {e}")
        return None, f"Repository tagging failed: {e}"

    storage_url = f"{REGISTRY_URL}/store_model"
    store_data = {"model_name": model_name}
    try:
        store_response = requests.post(storage_url, json=store_data)
        store_response.raise_for_status()
    except requests.exceptions.RequestException as e:
        log_message(model_name, f"Storing model information failed: {e}")
        return None, f"Storing model information failed: {e}"

    return release_info, None


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/upload", methods=["POST"])
def upload_model():
    """Handles the upload of model release packages and deploys the model."""
    required_files = ["web_app", "inference_app"]
    for key in required_files:
        if key not in request.files:
            return f"No file provided for {key}", 400

    model_name = request.form.get("model_name", "").strip()
    if not model_name:
        return "Model name is required", 400

    web_app_file = request.files["web_app"]
    inference_app_file = request.files["inference_app"]

    if web_app_file.filename == "" or inference_app_file.filename == "":
        return "One or more files were not selected", 400

    release_info, error = tag_and_store_release(model_name, web_app_file, inference_app_file)
    if error:
        return error, 500

    port_no = release_info.get("port_no")
    if port_no is None:
        return "Model deployment failed, no port assigned", 500

    deployment_response = deploy_model(model_name)
    if "error" in deployment_response:
        return "Model deployment failed", 500

    return redirect(url_for("list_models"))


@app.route("/list_models")
def list_models():
    """Lists all deployed models."""
    models = get_applications()
    if models is None:
        return "Failed to retrieve models", 500
    return render_template("list_models.html", models=models)


def find_model_info(model_id, models):
    """Helper to find a model in the applications list by its name."""
    return next((m for m in models if m.get("model_name") == model_id), None)


@app.route("/model/<model_id>", methods=["GET"])
def get_model(model_id):
    """Retrieves info for a specific model."""
    models = get_applications()
    if models is None:
        return f"Error retrieving models", 500

    model_info = find_model_info(model_id, models)
    if model_info:
        port = model_info.get("port_no")
        ip_address = model_info.get("ip_address", "127.0.0.1")
        if not port:
            return f"Running model {model_id} does not have an assigned port", 500
        return render_template("model_interface.html",
                               model_name=model_id, port=port, ip_address=ip_address)
    else:
        deployment_response = deploy_model(model_id)
        if "error" in deployment_response:
            return f"Deployment error for model {model_id}", 500
        port = deployment_response.get("port_no")
        ip_address = deployment_response.get("ip_address", "127.0.0.1")
        if not port:
            return f"Deployment did not assign a port for model {model_id}", 500
        return render_template("model_interface.html",
                               model_name=model_id, port=port, ip_address=ip_address)


@app.route("/model/<model_name>/instances")
def get_instances_model(model_name):
    """Retrieves instances for a specified model."""
    models = get_applications()
    if models is None:
        return f"Error retrieving models", 500

    model_info = find_model_info(model_name, models)
    if model_info:
        instances = model_info.get("instances", [])
        return render_template("instances_model.html", model_name=model_name, instances=instances)
    else:
        return f"No instances found for model {model_name}", 404


@app.route("/model/<model_name>/predict", methods=["POST"])
def reverse_proxy(model_name):
    """Proxies a prediction request to the model's prediction endpoint."""
    try:
        models = get_applications()
        if models is None:
            return "Error retrieving models", 500

        model_info = find_model_info(model_name, models)
        if model_info:
            port = model_info.get("port_no")
            if not port:
                return f"Model {model_name} is running but no port is assigned.", 500
            ip_address = model_info.get("ip_address", "127.0.0.1")
        else:
            deployment_response = deploy_model(model_name)
            if "error" in deployment_response:
                return f"Deployment error for model {model_name}: {deployment_response['error']}", 500
            port = deployment_response.get("port_no")
            ip_address = deployment_response.get("ip_address", "127.0.0.1")
            if not port:
                return f"Deployment did not assign a port for model {model_name}.", 500

        target_url = f"http://{ip_address}:{port}/"

        json_payload = request.get_json(force=True, silent=True)
        if json_payload is None:
            json_payload = request.form.to_dict()

        prediction_response = requests.post(target_url, json=json_payload)
        prediction_response.raise_for_status()

        return Response(
            prediction_response.content,
            status=prediction_response.status_code,
            mimetype=prediction_response.headers.get("Content-Type", "application/json")
        )
    except Exception as e:
        log_message(model_name, f"Prediction failed: {e}")
        return f"Prediction failed: {e}", 500


if __name__ == "__main__":
    app.run(debug=True, port=os.environ.get('port', 5000))
