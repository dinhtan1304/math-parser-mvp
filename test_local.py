"""
Test script for Math Parser MVP
Run: python test_local.py
"""

import asyncio
import json
import sys

# Add project to path
sys.path.insert(0, '.')

from file_handler import FileHandler
from ai_parser import AIQuestionParser


async def test_file_handler():
    """Test file extraction"""
    print("=" * 50)
    print("Testing FileHandler...")
    print("=" * 50)
    
    handler = FileHandler()
    
    # Test with sample file
    result = await handler.extract_text("sample_exam.txt")
    
    print(f"✅ File type: {result['file_type']}")
    print(f"✅ Hash: {result['file_hash']}")
    print(f"✅ Text length: {len(result['text'])} chars")
    print(f"\n📄 First 500 chars:\n{result['text'][:500]}...")
    
    return result['text']


async def test_ai_parser(text: str):
    """Test AI parsing"""
    print("\n" + "=" * 50)
    print("Testing AIQuestionParser...")
    print("=" * 50)
    
    parser = AIQuestionParser()
    
    questions = await parser.parse(text)
    
    print(f"\n✅ Found {len(questions)} questions\n")
    
    for i, q in enumerate(questions, 1):
        print(f"--- Câu {i} ---")
        print(f"📝 Question: {q['question'][:100]}...")
        print(f"📌 Type: {q['type']}")
        print(f"📚 Topic: {q['topic']}")
        print(f"⭐ Difficulty: {q['difficulty']}")
        print(f"✅ Answer: {q['answer'] or '(không có)'}")
        if q['solution_steps']:
            print(f"📖 Steps: {len(q['solution_steps'])} bước")
        print()
    
    # Save to file
    output_file = "output_questions.json"
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(questions, f, ensure_ascii=False, indent=2)
    
    print(f"💾 Saved to {output_file}")
    
    return questions


async def test_api():
    """Test API endpoints"""
    print("\n" + "=" * 50)
    print("Testing API (requires server running)...")
    print("=" * 50)
    
    try:
        import httpx
    except ImportError:
        print("⚠️ httpx not installed. Skipping API test.")
        print("Install with: pip install httpx")
        return
    
    async with httpx.AsyncClient() as client:
        try:
            # Health check
            res = await client.get("http://localhost:8000/health", timeout=5)
            print(f"✅ Health check: {res.json()}")
            
            # Upload file
            print("\n📤 Uploading sample file...")
            with open("sample_exam.txt", "rb") as f:
                res = await client.post(
                    "http://localhost:8000/api/parse",
                    files={"file": f}
                )
            
            data = res.json()
            job_id = data["job_id"]
            print(f"✅ Job created: {job_id}")
            
            # Poll for result
            print("⏳ Waiting for processing...")
            for _ in range(30):
                res = await client.get(f"http://localhost:8000/api/status/{job_id}")
                status = res.json()
                
                if status["status"] == "completed":
                    print(f"✅ Completed! Found {len(status['result'])} questions")
                    break
                elif status["status"] == "failed":
                    print(f"❌ Failed: {status['error']}")
                    break
                else:
                    print(f"   Progress: {status['progress']}%")
                    await asyncio.sleep(1)
            
        except httpx.ConnectError:
            print("⚠️ Server not running. Start with: python main.py")
        except Exception as e:
            print(f"❌ Error: {e}")


async def main():
    print("\n🚀 Math Parser MVP - Test Suite\n")
    
    # Test 1: File Handler
    text = await test_file_handler()
    
    # Test 2: AI Parser
    questions = await test_ai_parser(text)
    
    # Test 3: API (optional)
    await test_api()
    
    print("\n" + "=" * 50)
    print("✅ All tests completed!")
    print("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())