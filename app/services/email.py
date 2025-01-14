import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import List, Optional
from datetime import datetime

from app.core.config import settings
from app.models.order import Order, OrderStatus
from app.models.product import Product

class EmailService:
    def __init__(self):
        self.smtp_host = settings.SMTP_HOST
        self.smtp_port = settings.SMTP_PORT
        self.smtp_user = settings.SMTP_USER
        self.smtp_password = settings.SMTP_PASSWORD
        self.from_email = settings.EMAILS_FROM_EMAIL
        self.from_name = settings.EMAILS_FROM_NAME

    def _create_message(self, to_email: str, subject: str, html_content: str) -> MIMEMultipart:
        message = MIMEMultipart('alternative')
        message['Subject'] = subject
        message['From'] = f"{self.from_name} <{self.from_email}>"
        message['To'] = to_email
        message.attach(MIMEText(html_content, 'html'))
        return message

    def _send_email(self, to_email: str, subject: str, html_content: str) -> bool:
        try:
            message = self._create_message(to_email, subject, html_content)
            with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                server.starttls()
                server.login(self.smtp_user, self.smtp_password)
                server.send_message(message)
            return True
        except Exception as e:
            print(f"Failed to send email: {str(e)}")
            return False

def send_order_confirmation_email(to_email: str, order: Order) -> bool:
    email_service = EmailService()
    
    items_html = "".join([
        f"<tr><td>{item.product.name}</td><td>{item.quantity}</td><td>${item.price_at_time}</td></tr>"
        for item in order.items
    ])
    
    html_content = f"""
    <html>
        <body>
            <h2>Order Confirmation #{order.id}</h2>
            <p>Thank you for your order!</p>
            
            <h3>Order Details:</h3>
            <table border="1">
                <tr>
                    <th>Product</th>
                    <th>Quantity</th>
                    <th>Price</th>
                </tr>
                {items_html}
            </table>
            
            <p>Total Amount: ${order.total_amount}</p>
            <p>Order Status: {order.status.value}</p>
            
            <p>Order Date: {order.created_at.strftime('%Y-%m-%d %H:%M:%S')}</p>
            
            <p>Thank you for shopping with us!</p>
        </body>
    </html>
    """
    
    return email_service._send_email(
        to_email,
        f"Order Confirmation #{order.id}",
        html_content
    )

def send_order_status_update_email(to_email: str, order: Order) -> bool:
    email_service = EmailService()
    
    html_content = f"""
    <html>
        <body>
            <h2>Order Status Update</h2>
            <p>Your order #{order.id} has been updated.</p>
            
            <p>New Status: <strong>{order.status.value}</strong></p>
            
            <h3>Order Details:</h3>
            <p>Order Date: {order.created_at.strftime('%Y-%m-%d %H:%M:%S')}</p>
            <p>Total Amount: ${order.total_amount}</p>
            
            <p>Thank you for your patience!</p>
        </body>
    </html>
    """
    
    return email_service._send_email(
        to_email,
        f"Order Status Update - #{order.id}",
        html_content
    )

def send_order_cancellation_email(to_email: str, order: Order) -> bool:
    email_service = EmailService()
    
    html_content = f"""
    <html>
        <body>
            <h2>Order Cancellation</h2>
            <p>Your order #{order.id} has been cancelled.</p>
            
            <h3>Order Details:</h3>
            <p>Order Date: {order.created_at.strftime('%Y-%m-%d %H:%M:%S')}</p>
            <p>Total Amount: ${order.total_amount}</p>
            
            <p>If you have any questions, please contact our support team.</p>
        </body>
    </html>
    """
    
    return email_service._send_email(
        to_email,
        f"Order Cancellation - #{order.id}",
        html_content
    )

def send_low_stock_alert_email(to_email: str, products: List[Product]) -> bool:
    email_service = EmailService()
    
    products_html = "".join([
        f"<tr><td>{product.name}</td><td>{product.stock_quantity}</td></tr>"
        for product in products
    ])
    
    html_content = f"""
    <html>
        <body>
            <h2>Low Stock Alert</h2>
            <p>The following products are running low on stock:</p>
            
            <table border="1">
                <tr>
                    <th>Product</th>
                    <th>Current Stock</th>
                </tr>
                {products_html}
            </table>
            
            <p>Please review and restock as needed.</p>
            <p>Generated at: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}</p>
        </body>
    </html>
    """
    
    return email_service._send_email(
        to_email,
        "Low Stock Alert",
        html_content
    )

def send_welcome_email(to_email: str, full_name: str, verification_token: str) -> bool:
    email_service = EmailService()
    

    base_url = f"http://localhost:{settings.PORT}"
    verification_link = f"{base_url}{settings.API_V1_STR}/users/verify-email/{verification_token}"
    
    html_content = f"""
    <html>
        <body>
            <h2>Welcome to {settings.PROJECT_NAME}!</h2>
            <p>Dear {full_name},</p>
            
            <p>Thank you for registering with us. We're excited to have you as a member of our community!</p>
            
            <p><strong>Please verify your email address by clicking the link below:</strong></p>
            <p><a href="{verification_link}">Verify Email Address</a></p>
            <p>This link will expire in {settings.EMAIL_VERIFICATION_TOKEN_EXPIRE_HOURS} hours.</p>
            
            <h3>What's Next?</h3>
            <ul>
                <li>Verify your email address</li>
                <li>Browse our product catalog</li>
                <li>Update your profile information</li>
                <li>Add items to your shopping cart</li>
            </ul>
            
            <p>If you have any questions, our support team is here to help.</p>
            
            <p>Best regards,<br>The {settings.PROJECT_NAME} Team</p>
        </body>
    </html>
    """
    
    return email_service._send_email(
        to_email,
        f"Welcome to {settings.PROJECT_NAME}! Please Verify Your Email",
        html_content
    )

def send_email_verification(to_email: str, verification_token: str) -> bool:
    email_service = EmailService()
    

    base_url = f"http://localhost:{settings.PORT}"
    verification_link = f"{base_url}{settings.API_V1_STR}/users/verify-email/{verification_token}"
    
    html_content = f"""
    <html>
        <body>
            <h2>Email Verification</h2>
            <p>Please verify your email address by clicking the link below:</p>
            
            <p><a href="{verification_link}">Verify Email Address</a></p>
            
            <p>This link will expire in {settings.EMAIL_VERIFICATION_TOKEN_EXPIRE_HOURS} hours.</p>
            
            <p>If you didn't request this verification, please ignore this email.</p>
            
            <p>Best regards,<br>The {settings.PROJECT_NAME} Team</p>
        </body>
    </html>
    """
    
    return email_service._send_email(
        to_email,
        "Verify Your Email Address",
        html_content
    )

def send_password_reset_email(to_email: str, reset_token: str) -> bool:
    email_service = EmailService()
    

    base_url = f"http://localhost:{settings.PORT}"
    reset_link = f"{base_url}{settings.API_V1_STR}/users/password/reset/{reset_token}"
    
    html_content = f"""
    <html>
        <body>
            <h2>Password Reset Request</h2>
            <p>You have requested to reset your password.</p>
            
            <p>Click the link below to reset your password:</p>
            <p><a href="{reset_link}">Reset Password</a></p>
            
            <p>This link will expire in 24 hours.</p>
            
            <p>If you didn't request this, please ignore this email.</p>
            
            <p>Best regards,<br>The {settings.PROJECT_NAME} Team</p>
        </body>
    </html>
    """
    
    return email_service._send_email(
        to_email,
        "Password Reset Request",
        html_content
    )

def send_gdpr_export_email(to_email: str, export_data: "GDPRExport") -> bool:
    email_service = EmailService()
    
    html_content = f"""
    <html>
        <body>
            <h2>Your Personal Data Export (GDPR Article 15)</h2>
            <p>As requested under your right of access, here is your personal data export.</p>
            
            <h3>Export Details:</h3>
            <ul>
                <li>Request Date: {export_data.request_date.strftime('%Y-%m-%d %H:%M:%S UTC')}</li>
                <li>Generated at: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}</li>
                <li>Expires at: {export_data.expires_at.strftime('%Y-%m-%d %H:%M:%S UTC')}</li>
                <li>Request ID: {export_data.request_id}</li>
            </ul>
            
            <p>For your privacy and security:</p>
            <ul>
                <li>This export will be automatically deleted after {settings.GDPR_EXPORT_EXPIRY_HOURS} hours</li>
                <li>Store this data securely and do not share it with others</li>
                <li>Use encryption when storing this data</li>
                <li>Delete this export once you no longer need it</li>
            </ul>
            
            <p>Questions about your data?</p>
            <ul>
                <li>Contact our DPO: {settings.DPO_EMAIL}</li>
                <li>Visit our Privacy Policy: [Link to Privacy Policy]</li>
                <li>Your rights under GDPR: [Link to Rights Page]</li>
            </ul>
            
            <p>Best regards,<br>{settings.PROJECT_NAME}</p>
            <p>Data Protection Officer: {settings.DPO_NAME}</p>
            <p>{settings.COMPANY_ADDRESS}</p>
        </body>
    </html>
    """
    
    return email_service._send_email(
        to_email,
        "Your Personal Data Export (GDPR Request)",
        html_content
    )

def send_gdpr_deletion_confirmation(to_email: str, request_id: str) -> bool:
    email_service = EmailService()
    
    html_content = f"""
    <html>
        <body>
            <h2>Data Deletion Confirmation (GDPR Article 17)</h2>
            <p>We confirm that your personal data has been deleted as requested.</p>
            
            <h3>Deletion Details:</h3>
            <ul>
                <li>Request ID: {request_id}</li>
                <li>Completed at: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}</li>
            </ul>
            
            <p>What was deleted:</p>
            <ul>
                <li>Account information</li>
                <li>Order history</li>
                <li>Communication preferences</li>
                <li>Addresses</li>
                <li>Usage data</li>
            </ul>
            
            <p>Please note:</p>
            <ul>
                <li>Some data may be retained for legal requirements (e.g., tax records)</li>
                <li>Anonymized data may be kept for analytics</li>
                <li>You can create a new account at any time</li>
            </ul>
            
            <p>Questions?</p>
            <p>Contact our DPO at {settings.DPO_EMAIL}</p>
            
            <p>Best regards,<br>{settings.PROJECT_NAME}</p>
            <p>Data Protection Officer: {settings.DPO_NAME}</p>
            <p>{settings.COMPANY_ADDRESS}</p>
        </body>
    </html>
    """
    
    return email_service._send_email(
        to_email,
        "Data Deletion Confirmation (GDPR Request)",
        html_content
    )

def send_gdpr_request_received(to_email: str, request_type: str, request_id: str) -> bool:
    email_service = EmailService()
    
    request_type_text = "deletion" if request_type == "deletion" else "export"
    gdpr_article = "17" if request_type == "deletion" else "15"
    
    html_content = f"""
    <html>
        <body>
            <h2>GDPR Request Received (Article {gdpr_article})</h2>
            <p>We have received your request for data {request_type_text}.</p>
            
            <h3>Request Details:</h3>
            <ul>
                <li>Request Type: {request_type_text}</li>
                <li>Request ID: {request_id}</li>
                <li>Received at: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}</li>
                <li>Estimated completion: Within 30 days</li>
            </ul>
            
            <p>Next steps:</p>
            <ul>
                <li>We will process your request within 30 days</li>
                <li>You will receive a confirmation email when completed</li>
                <li>We may contact you if we need additional information</li>
            </ul>
            
            <p>Your rights under GDPR:</p>
            <ul>
                <li>Right to access your data (Article 15)</li>
                <li>Right to erasure (Article 17)</li>
                <li>Right to data portability (Article 20)</li>
                <li>Right to object (Article 21)</li>
            </ul>
            
            <p>Questions about your request?</p>
            <p>Contact our DPO at {settings.DPO_EMAIL}</p>
            
            <p>Best regards,<br>{settings.PROJECT_NAME}</p>
            <p>Data Protection Officer: {settings.DPO_NAME}</p>
            <p>{settings.COMPANY_ADDRESS}</p>
        </body>
    </html>
    """
    
    return email_service._send_email(
        to_email,
        f"GDPR {request_type.title()} Request Received",
        html_content
    )

from app.schemas.user import GDPRExport