"""File-based storage backend implementation."""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from datetime import datetime
import uuid

from . import StorageBackend


class FileStorageBackend(StorageBackend):
    """File-based storage using JSON files.
    
    Directory structure:
    storage_dir/
    ├── portfolios.json (metadata)
    └── portfolio_{id}/
        ├── state.json
        ├── positions/
        │   └── {symbol}_{type}.json
        └── trades/
            └── {trade_id}.json
    """
    
    def __init__(self, storage_dir: str | Path = None):
        """Initialize file storage backend.
        
        Args:
            storage_dir: Root directory for storage. Defaults to .data
        """
        if storage_dir is None:
            storage_dir = Path(".data")
        
        self.storage_dir = Path(storage_dir)
        self.portfolios_file = self.storage_dir / "portfolios.json"
    
    def initialize(self) -> None:
        """Create storage directory structure."""
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        
        if not self.portfolios_file.exists():
            self._save_json(self.portfolios_file, {})
    
    def save_portfolio_state(self, portfolio_id: str, state_data: Dict[str, Any]) -> None:
        """Save portfolio state to JSON file."""
        portfolio_dir = self.storage_dir / f"portfolio_{portfolio_id}"
        portfolio_dir.mkdir(parents=True, exist_ok=True)
        
        state_file = portfolio_dir / "state.json"
        state_data['last_updated'] = datetime.now().isoformat()
        self._save_json(state_file, state_data)
        
        # Update portfolio metadata
        self._update_portfolio_metadata(portfolio_id, state_data)
    
    def load_portfolio_state(self, portfolio_id: str) -> Optional[Dict[str, Any]]:
        """Load portfolio state from JSON file."""
        state_file = self.storage_dir / f"portfolio_{portfolio_id}" / "state.json"
        
        if not state_file.exists():
            return None
        
        return self._load_json(state_file)
    
    def save_trade(self, portfolio_id: str, trade_data: Dict[str, Any]) -> str:
        """Save trade record to JSON file."""
        portfolio_dir = self.storage_dir / f"portfolio_{portfolio_id}"
        trades_dir = portfolio_dir / "trades"
        trades_dir.mkdir(parents=True, exist_ok=True)
        
        trade_id = trade_data.get('id') or str(uuid.uuid4())
        trade_data['id'] = trade_id
        
        trade_file = trades_dir / f"{trade_id}.json"
        self._save_json(trade_file, trade_data)
        
        return trade_id
    
    def load_trades(self, portfolio_id: str, limit: int = None) -> List[Dict[str, Any]]:
        """Load trades for a portfolio from JSON files."""
        trades_dir = self.storage_dir / f"portfolio_{portfolio_id}" / "trades"
        
        if not trades_dir.exists():
            return []
        
        trades = []
        for trade_file in trades_dir.glob("*.json"):
            trade = self._load_json(trade_file)
            if trade:
                trades.append(trade)
        
        # Sort by timestamp descending
        trades.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
        
        if limit:
            trades = trades[:limit]
        
        return trades
    
    def save_position(self, portfolio_id: str, position_data: Dict[str, Any]) -> None:
        """Save position information to JSON file."""
        portfolio_dir = self.storage_dir / f"portfolio_{portfolio_id}"
        positions_dir = portfolio_dir / "positions"
        positions_dir.mkdir(parents=True, exist_ok=True)
        
        symbol = position_data.get('symbol')
        pos_type = position_data.get('type')
        position_file = positions_dir / f"{symbol}_{pos_type}.json"
        
        self._save_json(position_file, position_data)
    
    def load_positions(self, portfolio_id: str) -> List[Dict[str, Any]]:
        """Load all positions for a portfolio from JSON files."""
        positions_dir = self.storage_dir / f"portfolio_{portfolio_id}" / "positions"
        
        if not positions_dir.exists():
            return []
        
        positions = []
        for position_file in positions_dir.glob("*.json"):
            position = self._load_json(position_file)
            if position:
                positions.append(position)
        
        return positions
    
    def delete_portfolio(self, portfolio_id: str) -> None:
        """Delete portfolio directory and all its data."""
        portfolio_dir = self.storage_dir / f"portfolio_{portfolio_id}"
        
        if portfolio_dir.exists():
            import shutil
            shutil.rmtree(portfolio_dir)
        
        # Update metadata
        portfolios = self._load_json(self.portfolios_file) or {}
        if portfolio_id in portfolios:
            del portfolios[portfolio_id]
            self._save_json(self.portfolios_file, portfolios)
    
    def get_portfolio_list(self) -> List[Dict[str, Any]]:
        """Get list of all portfolios from metadata file."""
        portfolios = self._load_json(self.portfolios_file) or {}
        return list(portfolios.values())
    
    # Helper methods
    
    def _save_json(self, file_path: Path, data: Dict[str, Any]) -> None:
        """Save data to JSON file."""
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(file_path, 'w') as f:
            json.dump(data, f, indent=2, default=str)
    
    def _load_json(self, file_path: Path) -> Optional[Dict[str, Any]]:
        """Load data from JSON file."""
        if not file_path.exists():
            return None
        
        try:
            with open(file_path, 'r') as f:
                return json.load(f)
        except Exception:
            return None
    
    def _update_portfolio_metadata(self, portfolio_id: str, state_data: Dict[str, Any]) -> None:
        """Update portfolio metadata in portfolios.json."""
        portfolios = self._load_json(self.portfolios_file) or {}
        
        portfolios[portfolio_id] = {
            'id': portfolio_id,
            'created_at': portfolios.get(portfolio_id, {}).get('created_at', datetime.now().isoformat()),
            'last_updated': datetime.now().isoformat(),
            'capital': state_data.get('capital', 0.0),
            'current_pnl': state_data.get('total_pnl', 0.0),
        }
        
        self._save_json(self.portfolios_file, portfolios)
