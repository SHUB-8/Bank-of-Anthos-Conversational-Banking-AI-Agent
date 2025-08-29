from flask import Flask, request, jsonify
import jwt
import requests
import smtplib
from email.mime.text import MIMEText
import os
import random
import redis

app = Flask(__name__)

# Replace with your actual configuration
app.config['JWT_SECRET_KEY'] = 'your-jwt-secret'
userservice_url = "http://userservice:6000"
ledgerwriter_url = "http://ledgerwriter:6000"

redis_client = redis.StrictRedis(host='redis', port=6379, db=0)

def get_account_by_email(email):
    # This function will need to be implemented to query the userservice
    # or a shared database to get the account number associated with an email.
    # For now, we will use a placeholder.
    if email == "user@example.com":
        return "1234567890"
    return None

@app.route('/payment/initiate', methods=['POST'])
def initiate_payment():
    data = request.get_json()
    payer_email = data['payerEmail']
    invoice_id = data['invoiceId']
    amount = data['amount']

    # Generate and store OTP
    otp = str(random.randint(100000, 999999))
    redis_client.setex(payer_email, 300, f'{otp}:{invoice_id}:{amount}')

    # Send OTP to user's email
    msg = MIMEText(f'Your OTP is: {otp}')
    msg['Subject'] = 'Your One-Time Password'
    msg['From'] = 'noreply@yourbank.com'
    msg['To'] = payer_email
    # Configure your SMTP server
    with smtplib.SMTP('smtp.gmail.com', 587) as server:
        server.starttls()
        server.login("your_email@gmail.com", "your_password")
        server.send_message(msg)

    return jsonify({'message': 'OTP sent successfully'}), 200

@app.route('/payment/confirm', methods=['POST'])
def confirm_payment():
    data = request.get_json()
    payer_email = data['payerEmail']
    otp_attempt = data['otp']

    # Retrieve and validate OTP
    stored_data = redis_client.get(payer_email)
    if not stored_data:
        return jsonify({'error': 'OTP expired or invalid'}), 400

    stored_otp, invoice_id, amount = stored_data.decode().split(':')
    if otp_attempt != stored_otp:
        return jsonify({'error': 'Invalid OTP'}), 400

    # Get payer and payee account numbers
    payer_account = get_account_by_email(payer_email)
    payee_email = "sender@example.com"  # This should be extracted from the invoice
    payee_account = get_account_by_email(payee_email)

    if not payer_account or not payee_account:
        return jsonify({'error': 'Invalid payer or payee email'}), 400

    # Create transaction
    transaction_data = {
        "from_account": payer_account,
        "to_account": payee_account,
        "amount": int(amount)
    }
    response = requests.post(f"{ledgerwriter_url}/transactions", json=transaction_data)

    if response.status_code == 200:
        return jsonify({'message': 'Payment successful'}), 200
    else:
        return jsonify({'error': 'Payment failed'}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=6000)
