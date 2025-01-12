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
            <h2>Your Data Export</h2>
            <p>As requested, here is your personal data export.</p>
            
            <h3>Export Details:</h3>
            <ul>
                <li>Generated at: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}</li>
                <li>Expires at: {export_data.expires_at.strftime('%Y-%m-%d %H:%M:%S UTC')}</li>
            </ul>
            
            <p>For your privacy and security:</p>
            <ul>
                <li>This export will be automatically deleted after {settings.GDPR_EXPORT_EXPIRY_HOURS} hours</li>
                <li>Store this data securely and do not share it with others</li>
                <li>Contact us if you have any questions about your data</li>
            </ul>
            
            <p>Best regards,<br>The {settings.PROJECT_NAME} Team</p>
        </body>
    </html>
    """
    
    return email_service._send_email(
        to_email,
        "Your Data Export",
        html_content
    )

from app.schemas.user import GDPRExport