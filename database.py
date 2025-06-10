import os
import time
import sqlite3
import logging
import shutil
import json
from typing import Optional, Dict, List
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

# Import config for admin check
from config import get_config

# Load environment variables
load_dotenv()

# Constants
DB_PATH = "diamond_store.db"
DAMASCUS_TZ = timezone(timedelta(hours=3))

# Logger setup
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Date and time adapters for SQLite
def adapt_datetime(dt):
    """Convert datetime to a SQLite-compatible format."""
    return dt.isoformat()

def convert_datetime(text):
    """Convert text from SQLite to datetime."""
    return datetime.fromisoformat(text)

# Register the adapters
sqlite3.register_adapter(datetime, adapt_datetime)
sqlite3.register_converter("timestamp", convert_datetime)

class Database:
    """Database management class with improved error handling and connection management."""
    
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(Database, cls).__new__(cls)
            cls._instance.initialized = False
        return cls._instance

    def __init__(self):
        """Initialize the database if not already initialized."""
        if not self.initialized:
            self.initialized = True
            self.init_db()

    def get_connection(self):
        """Create a database connection with improved settings."""
        for i in range(5):  # Retry connection 5 times
            try:
                conn = sqlite3.connect(
                    DB_PATH,
                    timeout=30.0,
                    isolation_level=None,  # Enable manual transaction control
                    detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES
                )
                conn.execute('PRAGMA journal_mode=WAL')
                conn.execute('PRAGMA busy_timeout=30000')
                conn.row_factory = sqlite3.Row  # Enable row factory for named columns
                return conn
            except sqlite3.OperationalError as e:
                if i == 4:  # If all attempts failed
                    logger.error(f"Failed to connect to database after 5 attempts: {e}")
                    raise
                time.sleep(1)  # Wait before retrying

    def init_db(self):
        """Initialize the database with all required tables."""
        try:
            # Create backup directory if it doesn't exist
            os.makedirs('backup', exist_ok=True)

            # Backup existing database if it exists
            if os.path.exists(DB_PATH):
                backup_path = f'backup/diamond_store_{datetime.now().strftime("%Y%m%d_%H%M%S")}.db'
                shutil.copy2(DB_PATH, backup_path)
                logger.info(f"Database backed up to {backup_path}")

            conn = self.get_connection()
            c = conn.cursor()

            # Create tables with advanced settings
            c.executescript('''
                -- Enable foreign key constraints
                PRAGMA foreign_keys = ON;
                PRAGMA journal_mode = WAL;
                PRAGMA synchronous = NORMAL;
                PRAGMA cache_size = -2000;

                -- Users table with enhanced tracking
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    balance REAL DEFAULT 0.0 CHECK (balance >= 0),
                    joined_date TEXT NOT NULL,
                    last_activity TEXT NOT NULL,
                    status TEXT DEFAULT 'active' CHECK (status IN ('active', 'banned', 'suspended')),
                    account_data TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                );

                -- Balance history with improved tracking
                CREATE TABLE IF NOT EXISTS balance_history (
                    history_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    old_balance REAL NOT NULL,
                    new_balance REAL NOT NULL,
                    change_amount REAL NOT NULL,
                    transaction_type TEXT NOT NULL,
                    admin_id INTEGER,  -- Added admin_id for tracking who made the change
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
                );

                -- Orders table with enhanced tracking
                CREATE TABLE IF NOT EXISTS orders (
                    order_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    product_type TEXT NOT NULL CHECK (product_type IN ('game', 'app')),
                    product_id TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    price REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    status TEXT DEFAULT 'pending' CHECK (status IN ('pending', 'completed', 'rejected', 'expired')),
                    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
                );

                -- Transactions table with improved tracking
                CREATE TABLE IF NOT EXISTS transactions (
                    tx_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    amount REAL NOT NULL,
                    type TEXT NOT NULL CHECK (type IN ('deposit', 'withdrawal')),
                    payment_method TEXT NOT NULL,
                    payment_details TEXT,
                    payment_subtype TEXT,  -- For shamcash: 'syp' or 'usd'
                    payment_number TEXT,   -- The payment number usd
                    original_amount REAL,
                    original_currency TEXT,
                    exchange_rate REAL,  -- Added exchange rate tracking
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    status TEXT DEFAULT 'pending' CHECK (status IN ('pending', 'completed', 'rejected', 'expired')),
                    reject_reason TEXT,
                    admin_id INTEGER,  -- Added admin_id for tracking who processed the transaction
                    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
                );

                -- Admin logs with detailed tracking
                CREATE TABLE IF NOT EXISTS admin_logs (
                    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    admin_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    details TEXT,
                    target_user_id INTEGER,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (target_user_id) REFERENCES users(user_id) ON DELETE SET NULL
                );

                -- Create indices for performance
                CREATE INDEX IF NOT EXISTS idx_users_username ON users (username);
                CREATE INDEX IF NOT EXISTS idx_users_status ON users (status);
                CREATE INDEX IF NOT EXISTS idx_transactions_user_status ON transactions (user_id, status);
                CREATE INDEX IF NOT EXISTS idx_orders_user_status ON orders (user_id, status);
                CREATE INDEX IF NOT EXISTS idx_balance_history_user ON balance_history (user_id);
                CREATE INDEX IF NOT EXISTS idx_admin_logs_admin ON admin_logs (admin_id);
            ''')

            conn.commit()
            logger.info("Database initialized successfully")

        except Exception as e:
            logger.error(f"Database initialization error: {e}")
            raise
        finally:
            if conn:
                conn.close()

    async def is_admin(self, user_id: int) -> bool:
        """Check if a user is an admin."""
        config = get_config()
        return user_id in config.ADMINS

    async def get_user_stats(self, user_id: int) -> Optional[Dict]:
        """Get comprehensive user statistics."""
        conn = None
        try:
            conn = self.get_connection()
            c = conn.cursor()
            
            # Get user basic info
            c.execute("""
                SELECT balance, joined_date, last_activity, status,
                       created_at, username, first_name
                FROM users 
                WHERE user_id = ?
            """, (user_id,))
            user = c.fetchone()
            
            if not user:
                return None

            # Get transaction statistics
            c.execute("""
                SELECT 
                    COUNT(*) as total_tx,
                    COALESCE(SUM(CASE WHEN type = 'deposit' AND status = 'completed' THEN amount ELSE 0 END), 0) as deposits,
                    COALESCE(SUM(CASE WHEN type = 'withdrawal' AND status = 'completed' THEN amount ELSE 0 END), 0) as withdrawals
                FROM transactions 
                WHERE user_id = ?
            """, (user_id,))
            tx_stats = c.fetchone()

            # Get order statistics
            c.execute("""
                SELECT 
                    COUNT(*) as total_orders,
                    COALESCE(SUM(CASE WHEN status = 'completed' THEN price ELSE 0 END), 0) as total_spent
                FROM orders 
                WHERE user_id = ?
            """, (user_id,))
            order_stats = c.fetchone()

            return {
                'user_id': user_id,
                'username': user['username'],
                'first_name': user['first_name'],
                'current_balance': Decimal(str(user['balance'])),
                'join_date': datetime.fromisoformat(user['joined_date']),
                'last_active': datetime.fromisoformat(user['last_activity']),
                'is_banned': user['status'] == 'banned',
                'created_at': datetime.fromisoformat(user['created_at']),
                'total_transactions': tx_stats['total_tx'],
                'total_deposits': Decimal(str(tx_stats['deposits'])),
                'total_withdrawals': Decimal(str(tx_stats['withdrawals'])),
                'total_orders': order_stats['total_orders'],
                'total_spent': Decimal(str(order_stats['total_spent']))
            }

        except Exception as e:
            logger.error(f"Error getting user stats: {e}")
            return None
        finally:
            if conn:
                conn.close()

    async def ban_user(self, user_id: int, admin_id: int) -> bool:
        """Ban a user."""
        conn = None
        try:
            conn = self.get_connection()
            c = conn.cursor()
            
            c.execute("BEGIN TRANSACTION")
            
            # Update user status
            c.execute("""
                UPDATE users 
                SET status = 'banned', updated_at = CURRENT_TIMESTAMP 
                WHERE user_id = ?
            """, (user_id,))
            
            if c.rowcount > 0:
                # Log the action
                c.execute("""
                    INSERT INTO admin_logs (admin_id, action, details, target_user_id)
                    VALUES (?, 'ban_user', ?, ?)
                """, (admin_id, f"User {user_id} was banned", user_id))
                
                c.execute("COMMIT")
                return True
                
            c.execute("ROLLBACK")
            return False

        except Exception as e:
            if conn:
                c.execute("ROLLBACK")
            logger.error(f"Error banning user: {e}")
            return False
        finally:
            if conn:
                conn.close()

    async def unban_user(self, user_id: int, admin_id: int) -> bool:
        """Unban a user."""
        conn = None
        try:
            conn = self.get_connection()
            c = conn.cursor()
            
            c.execute("BEGIN TRANSACTION")
            
            # Update user status
            c.execute("""
                UPDATE users 
                SET status = 'active', updated_at = CURRENT_TIMESTAMP 
                WHERE user_id = ?
            """, (user_id,))
            
            if c.rowcount > 0:
                # Log the action
                c.execute("""
                    INSERT INTO admin_logs (admin_id, action, details, target_user_id)
                    VALUES (?, 'unban_user', ?, ?)
                """, (admin_id, f"User {user_id} was unbanned", user_id))
                
                c.execute("COMMIT")
                return True
                
            c.execute("ROLLBACK")
            return False

        except Exception as e:
            if conn:
                c.execute("ROLLBACK")
            logger.error(f"Error unbanning user: {e}")
            return False
        finally:
            if conn:
                conn.close()

    async def modify_user_balance(self, user_id: int, amount: Decimal, admin_id: int) -> bool:
        """Modify user balance."""
        conn = None
        try:
            conn = self.get_connection()
            c = conn.cursor()
            
            c.execute("BEGIN TRANSACTION")
            
            # Get current balance
            c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
            result = c.fetchone()
            if not result:
                c.execute("ROLLBACK")
                return False
                
            current_balance = Decimal(str(result['balance']))
            new_balance = current_balance + amount
            
            # Prevent negative balance
            if new_balance < 0:
                c.execute("ROLLBACK")
                return False
            
            # Update balance
            c.execute("""
                UPDATE users 
                SET balance = ?, updated_at = CURRENT_TIMESTAMP 
                WHERE user_id = ?
            """, (str(new_balance), user_id))
            
            # Log the change
            c.execute("""
                INSERT INTO balance_history 
                (user_id, old_balance, new_balance, change_amount, transaction_type, admin_id)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (user_id, str(current_balance), str(new_balance), str(amount),
                  'credit' if amount > 0 else 'debit', admin_id))
            
            # Log admin action
            c.execute("""
                INSERT INTO admin_logs (admin_id, action, details, target_user_id)
                VALUES (?, 'modify_balance', ?, ?)
            """, (admin_id, f"Modified balance by {amount}", user_id))
            
            c.execute("COMMIT")
            return True

        except Exception as e:
            if conn:
                c.execute("ROLLBACK")
            logger.error(f"Error modifying balance: {e}")
            return False
        finally:
            if conn:
                conn.close()

    async def get_user_id_by_username(self, username: str) -> Optional[int]:
        """Get user ID from username."""
        conn = None
        try:
            conn = self.get_connection()
            c = conn.cursor()
            
            username = username.lstrip('@')
            c.execute("SELECT user_id FROM users WHERE username = ?", (username,))
            result = c.fetchone()
            return result['user_id'] if result else None

        except Exception as e:
            logger.error(f"Error getting user ID: {e}")
            return None
        finally:
            if conn:
                conn.close()

    async def get_total_users(self) -> int:
        """Get total number of users."""
        conn = None
        try:
            conn = self.get_connection()
            c = conn.cursor()
            c.execute("SELECT COUNT(*) as count FROM users")
            return c.fetchone()['count']
        except Exception as e:
            logger.error(f"Error getting total users: {e}")
            return 0
        finally:
            if conn:
                conn.close()

    async def get_active_users_last_24h(self) -> int:
        """Get number of active users in last 24 hours."""
        conn = None
        try:
            conn = self.get_connection()
            c = conn.cursor()
            c.execute("""
                SELECT COUNT(*) as count FROM users 
                WHERE datetime(last_activity) > datetime('now', '-1 day')
                AND status = 'active'
            """)
            return c.fetchone()['count']
        except Exception as e:
            logger.error(f"Error getting active users: {e}")
            return 0
        finally:
            if conn:
                conn.close()

    async def get_total_transaction_volume(self) -> Decimal:
        """Get total transaction volume."""
        conn = None
        try:
            conn = self.get_connection()
            c = conn.cursor()
            c.execute("""
                SELECT COALESCE(SUM(amount), 0) as total 
                FROM transactions 
                WHERE status = 'completed'
            """)
            return Decimal(str(c.fetchone()['total']))
        except Exception as e:
            logger.error(f"Error getting transaction volume: {e}")
            return Decimal('0')
     
    async def create_transaction(
        self,
        tx_id: str,
        user_id: int,
        amount: Decimal,
        payment_method: str,
        payment_subtype: Optional[str] = None,
        payment_number: Optional[str] = None,
        **kwargs
    ) -> bool:
        """Create a new transaction with enhanced details."""
        conn = None
        try:
            conn = self.get_connection()
            c = conn.cursor()
        
            c.execute("BEGIN TRANSACTION")
        
            c.execute("""
                INSERT INTO transactions (
                    tx_id, user_id, amount, type, payment_method,
                    payment_subtype, payment_number, payment_details,
                    original_amount, original_currency, exchange_rate,
                    created_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, 'pending')
            """, (
                tx_id, user_id, str(amount), 'deposit', payment_method,
                payment_subtype, payment_number,
                json.dumps(kwargs.get('payment_details', {})),
                str(kwargs.get('original_amount', amount)),
                kwargs.get('original_currency', 'SYP'),
                str(kwargs.get('exchange_rate', 1))
            ))
        
            c.execute("COMMIT")
            return True
        
        except Exception as e:
            if conn:
                c.execute("ROLLBACK")
            logger.error(f"Error creating transaction: {e}")
            return False
     
        finally:
            if conn:
                conn.close()

# Create a single instance
_db = Database()

def get_database() -> Database:
    """Get the Database instance."""
    return _db