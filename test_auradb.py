import os
from dotenv import load_dotenv
from neo4j import GraphDatabase

load_dotenv()

uri = os.getenv("NEO4J_uri")
user = os.getenv("NEO4J_user", "neo4j")
password = os.getenv("NEO4J_password")

if not uri or not password:
    print("❌ NEO4J_URI or NEO4J_PASSWORD missing in .env")
    exit(1)

print(f"🔌 Testing AuraDB: {uri}")

try:
    driver = GraphDatabase.driver(uri, auth=(user, password))
    driver.verify_connectivity()
    print("✅ SUCCESS: Connected to Neo4j AuraDB!")
    
    # Quick sanity query
    with driver.session() as session:
        result = session.run("RETURN 1 + 1 AS sum")
        print(f"📊 Sanity check: 1+1 = {result.single()['sum']}")
    
    driver.close()
    print("🎉 AuraDB is ready for Developer Farm integration!")
    
except Exception as e:
    print(f"❌ FAILED: {type(e).__name__}: {e}")
    print("\n🔧 Troubleshooting:")
    print("1. Check .env password (AuraDB shows it ONLY ONCE at creation)")
    print("2. Verify instance status in console (must be 'Running', not 'Paused')")
    print("3. Free tier allows all IPs; no whitelist needed")
