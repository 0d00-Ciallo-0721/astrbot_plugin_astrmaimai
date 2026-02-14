import time
from datetime import datetime

def get_relative_time_str(timestamp: float) -> str:
    """
    将时间戳转换为相对时间描述 (拟人化)
    用于构建 Time-Aware Context
    """
    if not timestamp:
        return ""
        
    delta = time.time() - timestamp
    
    if delta < 60:
        return "just now"
    elif delta < 3600:
        minutes = int(delta / 60)
        return f"{minutes}m ago"
    elif delta < 86400:
        hours = int(delta / 3600)
        return f"{hours}h ago"
    elif delta < 604800:
        days = int(delta / 86400)
        return f"{days}d ago"
    else:
        # 超过一周显示具体日期
        dt = datetime.fromtimestamp(timestamp)
        return dt.strftime("%m-%d")