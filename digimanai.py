from flask import Flask, request, jsonify
app = Flask(__name__)

@app.route("/digiman/command", methods=["POST"])
def digiman_command():
    data = request.json
    print("Received:", data)
    return jsonify({"status": "command received"})

@app.route("/digiman/insights", methods=["GET"])
def insights():
    return jsonify({"status": "up", "message": "Insights live"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001)
