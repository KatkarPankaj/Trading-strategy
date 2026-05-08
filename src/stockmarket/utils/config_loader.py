"""Configuration loader for JSON configuration files."""

import json
from pathlib import Path
from typing import Any, Dict

class ConfigLoader:
    """Loads and manages application configuration from JSON files."""
    
    def __init__(self, config_dir: str | Path = None):
        """Initialize config loader.
        
        Args:
            config_dir: Path to configuration directory. If None, uses './config'
        """
        if config_dir is None:
            config_dir = Path(__file__).parents[3] / "config"
        self.config_dir = Path(config_dir)
    
    def load(self, config_name: str) -> Dict[str, Any]:
        """Load configuration from JSON file.
        
        Args:
            config_name: Name of config file (e.g., 'app_config.json')
            
        Returns:
            Dictionary containing configuration
            
        Raises:
            FileNotFoundError: If config file doesn't exist
            json.JSONDecodeError: If config file is invalid JSON
        """
        config_path = self.config_dir / config_name
        
        if not config_path.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")
        
        with open(config_path, 'r') as f:
            return json.load(f)
    
    def load_all(self) -> Dict[str, Dict[str, Any]]:
        """Load all JSON configuration files from config directory.
        
        Returns:
            Dictionary mapping config names to their contents
        """
        configs = {}
        for config_file in self.config_dir.glob("*.json"):
            try:
                configs[config_file.stem] = self.load(config_file.name)
            except Exception as e:
                print(f"Warning: Failed to load {config_file.name}: {e}")
        return configs
    
    def get_market_config(self) -> Dict[str, Any]:
        """Get market configuration."""
        return self.load("market_config.json").get("markets", {})
    
    def get_app_config(self) -> Dict[str, Any]:
        """Get application configuration."""
        return self.load("app_config.json")
    
    def get_trading_config(self) -> Dict[str, Any]:
        """Get trading configuration."""
        return self.load("trading_config.json")
    
    def get_database_config(self) -> Dict[str, Any]:
        """Get database configuration."""
        return self.load("database_config.json")
