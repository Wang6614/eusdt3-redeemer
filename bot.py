import os, time, math, json, threading
from web3 import Web3
from web3.middleware import geth_poa_middleware

# ---------- 环境变量 ----------
RPC = os.getenv("RPC", "https://api.avax.network/ext/bc/C/rpc")  # AVAX C-Chain
PRIVATE_KEY = os.getenv("PRIVATE_KEY")  # 你的热钱包私钥（强烈建议新建干净小号）
WALLET = os.getenv("WALLET")            # 与私钥对应的钱包地址（0x开头）
VAULT = os.getenv("VAULT")              # Vault主合约地址（0xE1A62F...B2F824B）
EUSDT3 = os.getenv("EUSDT3")            # 你的 eUSDt-3 份额代币地址（LP代币地址）
USDT = os.getenv("USDT")                # 链上USDT地址（0x9702230A8Ea53601f5Cd2Dc00fDBc13D4dF4A8C7）
MIN_REDEEM_USDT = float(os.getenv("MIN_REDEEM_USDT", "10"))  # 触发阈值（USDT）
SHARES_TO_REDEEM = os.getenv("SHARES_TO_REDEEM", "ALL")      # 赎回份额：ALL 或 具体份额（整型字符串）
POLL_SEC = float(os.getenv("POLL_SEC", "0.2"))

# gas 策略
MAX_PRIORITY_GWEI = float(os.getenv("MAX_PRIORITY_GWEI", "2.0"))  # 提示费
MAX_FEE_GWEI      = float(os.getenv("MAX_FEE_GWEI", "40"))        # 封顶费，必要时自己调大
GAS_LIMIT_REDEEM  = int(os.getenv("GAS_LIMIT_REDEEM", "300000"))

# ---------- 基础 ----------
w3 = Web3(Web3.HTTPProvider(RPC, request_kwargs={"timeout": 20}))
w3.middleware_onion.inject(geth_poa_middleware, layer=0)
acct = w3.eth.account.from_key(PRIVATE_KEY)

# ---- 简易 ABI 片段 ----
ERC20_ABI = json.loads("""[
 {"constant":true,"inputs":[{"name":"a","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
 {"constant":true,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"stateMutability":"view","type":"function"},
 {"constant":true,"inputs":[{"name":"o","type":"address"},{"name":"s","type":"address"}],"name":"allowance","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
 {"constant":false,"inputs":[{"name":"s","type":"address"},{"name":"a","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"}
]""")

# ERC4626 常见接口
VAULT_4626_ABI = json.loads("""[
 {"inputs":[{"name":"shares","type":"uint256"},{"name":"receiver","type":"address"},{"name":"owner","type":"address"}],"name":"redeem","outputs":[{"name":"assets","type":"uint256"}],"stateMutability":"nonpayable","type":"function"},
 {"inputs":[{"name":"shares","type":"uint256"}],"name":"previewRedeem","outputs":[{"name":"assets","type":"uint256"}],"stateMutability":"view","type":"function"},
 {"inputs":[],"name":"asset","outputs":[{"name":"","type":"address"}],"stateMutability":"view","type":"function"}
]""")

# 兼容：有的“Vault Kit”只暴露 redeem(uint256)
VAULT_SIMPLE_ABI = json.loads("""[
 {"inputs":[{"name":"shares","type":"uint256"}],"name":"redeem","outputs":[],"stateMutability":"nonpayable","type":"function"}
]""")

usdt = w3.eth.contract(address=Web3.to_checksum_address(USDT), abi=ERC20_ABI)
lp   = w3.eth.contract(address=Web3.to_checksum_address(EUSDT3), abi=ERC20_ABI)

# 先尝试 4626 接口，失败再回退
try:
    vault = w3.eth.contract(address=Web3.to_checksum_address(VAULT), abi=VAULT_4626_ABI)
    _ = vault.functions.asset().call()
    USE_SIMPLE = False
except Exception:
    vault = w3.eth.contract(address=Web3.to_checksum_address(VAULT), abi=VAULT_SIMPLE_ABI)
    USE_SIMPLE = True

USDT_DEC = usdt.functions.decimals().call()
LP_DEC   = lp.functions.decimals().call()

def now_base_fees():
    base = w3.eth.gas_price          # 简化：用 gasPrice 近似 baseFee + priority
    prio = Web3.to_wei(MAX_PRIORITY_GWEI, "gwei")
    cap  = Web3.to_wei(MAX_FEE_GWEI, "gwei")
    # web3>=6建议用 EIP-1559 字段
    max_priority = prio
    max_fee = min(cap, max(base + prio*2, prio*2))
    return int(max_fee), int(max_priority)

def check_vault_usdt():
    bal = usdt.functions.balanceOf(Web3.to_checksum_address(VAULT)).call()
    return bal / (10**USDT_DEC)

def my_lp_shares():
    return lp.functions.balanceOf(Web3.to_checksum_address(WALLET)).call()

def ensure_allowance(shares):
    current = lp.functions.allowance(Web3.to_checksum_address(WALLET),
                                     Web3.to_checksum_address(VAULT)).call()
    if current >= shares:
        return
    tx = lp.functions.approve(Web3.to_checksum_address(VAULT), int(2**256-1)).build_transaction({
        "from": WALLET,
        "nonce": w3.eth.get_transaction_count(WALLET),
        "chainId": w3.eth.chain_id,
        "gas": 70000,
        "maxPriorityFeePerGas": now_base_fees()[1],
        "maxFeePerGas": now_base_fees()[0],
    })
    signed = w3.eth.account.sign_transaction(tx, private_key=PRIVATE_KEY)
    txh = w3.eth.send_raw_transaction(signed.rawTransaction)
    print("approve sent:", txh.hex())
    w3.eth.wait_for_transaction_receipt(txh, timeout=120)
    print("approve confirmed")

def redeem_once(shares_int):
    max_fee, max_prio = now_base_fees()
    nonce = w3.eth.get_transaction_count(WALLET)
    if not USE_SIMPLE:
        tx = vault.functions.redeem(shares_int, Web3.to_checksum_address(WALLET), Web3.to_checksum_address(WALLET)).build_transaction({
            "from": WALLET,
            "nonce": nonce,
            "chainId": w3.eth.chain_id,
            "gas": GAS_LIMIT_REDEEM,
            "maxPriorityFeePerGas": max_prio,
            "maxFeePerGas": max_fee,
        })
    else:
        tx = vault.functions.redeem(shares_int).build_transaction({
            "from": WALLET,
            "nonce": nonce,
            "chainId": w3.eth.chain_id,
            "gas": GAS_LIMIT_REDEEM,
            "maxPriorityFeePerGas": max_prio,
            "maxFeePerGas": max_fee,
        })
    signed = w3.eth.account.sign_transaction(tx, private_key=PRIVATE_KEY)
    txh = w3.eth.send_raw_transaction(signed.rawTransaction)
    print("redeem sent:", txh.hex())
    return txh

def loop():
    print("Bot started. Watching vault:", VAULT)
    while True:
        try:
            v_usdt = check_vault_usdt()
            if v_usdt >= MIN_REDEEM_USDT:
                # 计算准备赎回的份额
                shares = my_lp_shares()
                if shares == 0:
                    print("no LP shares, waiting...")
                    time.sleep(POLL_SEC); continue
                if SHARES_TO_REDEEM != "ALL":
                    shares = min(shares, int(SHARES_TO_REDEEM))

                # 预估可得（仅4626可用）
                if not USE_SIMPLE:
                    est = vault.functions.previewRedeem(shares).call() / (10**USDT_DEC)
                    if est < MIN_REDEEM_USDT:
                        # 份额太小，等更大余额
                        time.sleep(POLL_SEC); continue

                ensure_allowance(shares)
                txh = redeem_once(shares)
                # 简易等待结果（不阻塞后续轮询太久）
                try:
                    rcpt = w3.eth.wait_for_transaction_receipt(txh, timeout=45)
                    print("redeem done, status =", rcpt.status)
                except Exception as e:
                    print("wait receipt error:", e)
                    # 失败就继续顶替提交（相同nonce会被替换）— 由下一次循环处理
                    pass

            time.sleep(POLL_SEC)
        except Exception as e:
            print("err:", e)
            time.sleep(0.5)

if __name__ == "__main__":
    assert PRIVATE_KEY and WALLET and VAULT and EUSDT3 and USDT, "Env missing"
    loop()
