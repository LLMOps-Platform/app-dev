from flask import Flask, request, render_template_string
import requests
import os

app = Flask(__name__)

INDEX_HTML = """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <title>MNIST Digit Recognition</title>
    <style>
      #canvas {
        border: 1px solid black;
        background-color: black;
      }
    </style>
  </head>
  <body>
    <h1>MNIST Digit Recognition Web App</h1>
    <p>Draw a digit in the box below:</p>
    <canvas id="canvas" width="280" height="280"></canvas>
    <br><br>
    <button onclick="clearCanvas()">Clear</button>
    <button onclick="submitCanvas()">Predict</button>
    <form id="hiddenForm" method="post" action="/predict" style="display:none;">
      <input type="hidden" name="image_data" id="image_data">
    </form>
    {% if prediction is not none %}
      <h2>Prediction: {{ prediction }}</h2>
    {% endif %}
    <script>
      var canvas = document.getElementById('canvas');
      var ctx = canvas.getContext('2d');
      var drawing = false;

      // Set up drawing parameters
      ctx.lineWidth = 15;
      ctx.lineCap = 'round';
      ctx.strokeStyle = 'white';

      canvas.addEventListener('mousedown', function(e) {
        drawing = true;
        ctx.beginPath();
        ctx.moveTo(e.offsetX, e.offsetY);
      });

      canvas.addEventListener('mousemove', function(e) {
        if (drawing) {
          ctx.lineTo(e.offsetX, e.offsetY);
          ctx.stroke();
        }
      });

      canvas.addEventListener('mouseup', function(e) {
        drawing = false;
      });

      canvas.addEventListener('mouseout', function(e) {
        drawing = false;
      });

      function clearCanvas() {
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        // Fill with black background
        ctx.fillStyle = 'black';
        ctx.fillRect(0, 0, canvas.width, canvas.height);
      }

      function submitCanvas() {
        // Convert the canvas drawing to a base64 encoded PNG image.
        var dataURL = canvas.toDataURL('image/png');
        document.getElementById('image_data').value = dataURL;
        document.getElementById('hiddenForm').submit();
      }

      // Initialize canvas with black background
      clearCanvas();
    </script>
  </body>
</html>
"""

# URL for the inference API; can be overridden by an environment variable.
INFERENCE_API_URL = "http://localhost:5000/model/ocr_app/predict"

@app.route("/", methods=["GET"])
def index():
    return render_template_string(INDEX_HTML, prediction=None)

@app.route("/predict", methods=["POST"])
def predict():
    image_data = request.form.get("image_data")
    if not image_data:
        return render_template_string(INDEX_HTML, prediction="No drawing provided")
    
    try:
        # Call the inference API, sending the image data as JSON.
        response = requests.post(INFERENCE_API_URL, json={"image_data": image_data})
        if response.status_code == 200:
            result = response.json()
            prediction = result.get("prediction", "No prediction")
        else:
            prediction = f"Error: {response.text}"
    except Exception as e:
        prediction = f"Error contacting inference API: {str(e)}"
    
    return render_template_string(INDEX_HTML, prediction=prediction)

if __name__ == "__main__":
    # The port can be set via environment variable PORT or defaults to 5001.
    port = int(os.getenv("PORT", 5001))
    app.run(host="0.0.0.0", port=port)
