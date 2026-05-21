"""SQLite database storage backend implementation."""

import sqlite3
import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from datetime import datetime
import uuid

from . import StorageBackend


def open_sqlite_connection(db_path: str | Path, *, timeout: float = 10.0) -> sqlite3.Connection:
    """Open a SQLite connection with the project-wide pragmas.

    Shared by :class:`DatabaseStorageBackend` and the paper-trading SQLite
    repository so connection setup (WAL journal, busy timeout, row factory)
    lives in one place.
    """

    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


class DatabaseStorageBackend(StorageBackend):
    """SQLite database storage backend.
    
    Provides persistent storage with proper schema and indexing.
    Supports scaling to PostgreSQL in the future.
    """
    
    def __init__(self, db_path: str | Path = None):
        """Initialize database storage backend.
        
        Args:
            db_path: Path to SQLite database file. Defaults to .database/trading.db
        """
        if db_path is None:
            db_path = Path(".database") / "trading.db"
        
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
    
    def _get_connection(self) -> sqlite3.Connection:
        """Get database connection with proper configuration."""
        return open_sqlite_connection(self.db_path)
    
    def initialize(self) -> None:
        """Create database schema."""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            # Portfolios table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS portfolios (
                    id TEXT PRIMARY KEY,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    capital REAL NOT NULL,
                    current_pnl REAL DEFAULT 0.0
                )
            ''')
            
            # Portfolio state table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS portfolio_states (
                    portfolio_id TEXT PRIMARY KEY,
                    starting_capital REAL NOT NULL,
                    current_capital REAL NOT NULL,
                    total_pnl REAL DEFAULT 0.0,
                    state_json TEXT,
                    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(portfolio_id) REFERENCES portfolios(id)
                )
            ''')
            
            # Trades table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS trades (
                    id TEXT PRIMARY KEY,
                    portfolio_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    trade_type TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    entry_time TIMESTAMP NOT NULL,
                    qty INTEGER NOT NULL,
                    entry_charges REAL DEFAULT 0.0,
                    exit_price REAL,
                    exit_time TIMESTAMP,
                    exit_charges REAL DEFAULT 0.0,
                    pnl REAL DEFAULT 0.0,
                    pnl_pct REAL DEFAULT 0.0,
                    status TEXT DEFAULT 'OPEN',
                    trade_json TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(portfolio_id) REFERENCES portfolios(id)
                )
            ''')
            
            # Create index on portfolio_id and status
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_trades_portfolio 
                ON trades(portfolio_id)
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_trades_status 
                ON trades(status)
            ''')
            
            # Positions table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS positions (
                    id TEXT PRIMARY KEY,
                    portfolio_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    trade_type TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    avg_price REAL NOT NULL,
                    entry_charges REAL DEFAULT 0.0,
                    position_json TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(portfolio_id) REFERENCES portfolios(id)
                )
            ''')
            
            # Create unique index on portfolio, symbol, type
            cursor.execute('''
                CREATE UNIQUE INDEX IF NOT EXISTS idx_position_unique 
                ON positions(portfolio_id, symbol, trade_type)
            ''')
            
            conn.commit()
        
        finally:
            conn.close()
    
    def save_portfolio_state(self, portfolio_id: str, state_data: Dict[str, Any]) -> None:
        """Save portfolio state to database."""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            # Ensure portfolio exists
            cursor.execute(
                'INSERT OR IGNORE INTO portfolios (id, capital) VALUES (?, ?)',
                (portfolio_id, state_data.get('starting_capital', 0.0))
            )
            
            # Save state
            cursor.execute('''
                INSERT OR REPLACE INTO portfolio_states 
                (portfolio_id, starting_capital, current_capital, total_pnl, state_json, last_updated)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ''', (
                portfolio_id,
                state_data.get('starting_capital', 0.0),
                state_data.get('current_capital', 0.0),
                state_data.get('total_pnl', 0.0),
                json.dumps(state_data, default=str)
            ))
            
            conn.commit()
        
        finally:
            conn.close()
    
    def load_portfolio_state(self, portfolio_id: str) -> Optional[Dict[str, Any]]:
        """Load portfolio state from database."""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute(
                'SELECT state_json FROM portfolio_states WHERE portfolio_id = ?',
                (portfolio_id,)
            )
            row = cursor.fetchone()
            
            if row:
                return json.loads(row[0])
            return None
        
        finally:
            conn.close()
    
    def save_trade(self, portfolio_id: str, trade_data: Dict[str, Any]) -> str:
        """Save trade record to database."""
        trade_id = trade_data.get('id') or str(uuid.uuid4())
        trade_data['id'] = trade_id
        
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                INSERT OR REPLACE INTO trades 
                (id, portfolio_id, symbol, trade_type, entry_price, entry_time, qty,
                 entry_charges, exit_price, exit_time, exit_charges, pnl, pnl_pct, status, trade_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                trade_id,
                portfolio_id,
                trade_data.get('symbol'),
                trade_data.get('trade_type'),
                trade_data.get('entry_price'),
                trade_data.get('entry_time'),
                trade_data.get('qty'),
                trade_data.get('entry_charges', 0.0),
                trade_data.get('exit_price'),
                trade_data.get('exit_time'),
                trade_data.get('exit_charges', 0.0),
                trade_data.get('pnl', 0.0),
                trade_data.get('pnl_pct', 0.0),
                trade_data.get('status', 'OPEN'),
                json.dumps(trade_data, default=str)
            ))
            
            conn.commit()
            return trade_id
        
        finally:
            conn.close()
    
    def load_trades(self, portfolio_id: str, limit: int = None) -> List[Dict[str, Any]]:
        """Load trades from database."""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            query = '''
                SELECT trade_json FROM trades 
                WHERE portfolio_id = ? 
                ORDER BY timestamp DESC
            '''
            
            if limit:
                query += f' LIMIT {limit}'
            
            cursor.execute(query, (portfolio_id,))
            rows = cursor.fetchall()
            
            return [json.loads(row[0]) for row in rows]
        
        finally:
            conn.close()
    
    def save_position(self, portfolio_id: str, position_data: Dict[str, Any]) -> None:
        """Save position to database."""
        position_id = str(uuid.uuid4())
        
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                INSERT OR REPLACE INTO positions 
                (id, portfolio_id, symbol, trade_type, quantity, avg_price, entry_charges, position_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                position_id,
                portfolio_id,
                position_data.get('symbol'),
                position_data.get('type'),
                position_data.get('quantity'),
                position_data.get('avg_price'),
                position_data.get('entry_charges', 0.0),
                json.dumps(position_data, default=str)
            ))
            
            conn.commit()
        
        finally:
            conn.close()
    
    def load_positions(self, portfolio_id: str) -> List[Dict[str, Any]]:
        """Load positions from database."""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute(
                'SELECT position_json FROM positions WHERE portfolio_id = ?',
                (portfolio_id,)
            )
            rows = cursor.fetchall()
            
            return [json.loads(row[0]) for row in rows]
        
        finally:
            conn.close()
    
    def delete_portfolio(self, portfolio_id: str) -> None:
        """Delete portfolio and all associated data."""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute('DELETE FROM positions WHERE portfolio_id = ?', (portfolio_id,))
            cursor.execute('DELETE FROM trades WHERE portfolio_id = ?', (portfolio_id,))
            cursor.execute('DELETE FROM portfolio_states WHERE portfolio_id = ?', (portfolio_id,))
            cursor.execute('DELETE FROM portfolios WHERE id = ?', (portfolio_id,))
            conn.commit()
        
        finally:
            conn.close()
    
    def get_portfolio_list(self) -> List[Dict[str, Any]]:
        """Get list of all portfolios."""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                SELECT id, created_at, last_updated, capital, current_pnl 
                FROM portfolios 
                ORDER BY last_updated DESC
            ''')
            
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
        
        finally:
            conn.close()
