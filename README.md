# LLMOps
## OCR Model Integration and Deployment

**Name : Yash Bharatiya - (2024201020)**        
**Name : Yash Chordia   - (2024201029)**

### Overview
This project implements an OCR model with an integrated inference API and a simple web application. The system is designed to:
- Process images using the OCR model.
- Provide an inference API for OCR processing.
- Offer a web interface for interacting with the OCR API.
- Package all components for easy deployment.

### Deliverables
1. **OCR Model Implementation:**  
    The OCR model processes images to extract text. The model is integrated and can be updated or replaced as needed.

2. **Inference API:**  
    An API endpoint is provided to submit images and receive OCR results. This is built using a lightweight framework and integrated into the overall web application.

3. **Web Application Interface:**  
    A simple and user-friendly front-end allows users to upload images and view extracted text. The interface directly interacts with the inference API.

4. **Descriptor JSON:**  
    A descriptor JSON file contains all configuration details including:
    - Model name and version.
    - Required files such as `app.py`, OCR model weights, and `requirements.txt`.
    - Details for deployment including API endpoints and dependencies.

5. **Deployment Package:**  
    A zipped package that includes the OCR model, inference API, and web application. The package is ready for deployment on a different machine. It contains a complete set of setup instructions.

### Deployment Process (Deployer Role)
1. **Receiving the Package:**
    - The deployer receives a zipped deployment package.

2. **Extraction:**
    - Extract the package to a designated directory using a standard ZIP extractor.
    - Ensure that the file structure (with files like `app.py`, model weights, `requirements.txt`, and `descriptor.json`) remains intact.

3. **Environment Setup:**
    - Create a virtual environment:
      ```
      python -m venv venv
      source venv/bin/activate  # Linux/Mac
      venv\Scripts\activate     # Windows
      ```
    - Upgrade pip and install dependencies:
      ```
      pip install --upgrade pip
      pip install -r requirements.txt
      ```

4. **Running the Application:**
    - Launch the web application by running the entry script (e.g., `app.py`):
      ```
      python app.py
      ```
    - The application starts with both the web interface and OCR inference API available on the designated port.

5. **Verification:**
    - Access the web interface via a browser.
    - Test the OCR functionality by uploading an image and verifying the OCR output.

### Deployment Instructions and Setup Documentation
- **README:** This document outlines the overall architecture, deployment steps, and configurations.
- **Version Control:** The project supports version tagging and automated deployments. New versions are tagged automatically upon successful deployment.
- **Logging:** Deployment and instance logs are generated to assist in troubleshooting.

### Conclusion
This README provides a comprehensive guide to setting up and deploying the OCR model and its services. With clear instructions and a well-structured package, deploying the OCR system should be straightforward on any compatible machine.