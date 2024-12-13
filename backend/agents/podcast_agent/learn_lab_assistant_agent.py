import os
import json
import requests
import io
from typing import List, Dict, Any, Optional, TypedDict, Annotated, Sequence, Union, Literal
from pydantic import BaseModel, Field
from pydub import AudioSegment
from dotenv import load_dotenv
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage
from langgraph.graph import Graph, StateGraph, START, END
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain.output_parsers import PydanticOutputParser
from agents.utils.rag_application import RAGApplication
from datetime import datetime
from agents.utils.podcast_s3_storage import S3Storage
from agents.utils.upstash_cache import PodcastCache
from agents.utils.flashcard_agent import ContentEngine, FlashcardSet, Flashcard
from agents.utils.qna_agent import QuizGenerator, QuizSet


load_dotenv()

class RAGContext(BaseModel):   
    question: str
    pdf_title: str
    answer: Optional[str] = None
    evidence: List[str] = []

class PodcastSegment(BaseModel):
    speaker: str
    text: str
    expression: Optional[str] = None

class CacheResult(BaseModel):
    found: bool
    data: Optional[Dict[str, Any]] = None

class PodcastScript(BaseModel):
    segments: List[PodcastSegment] = Field(description="List of podcast segments")

class EnhancedGraphState(BaseModel):
    messages: List[BaseMessage]
    topic: str
    output_type: str
    rag_context: Optional[RAGContext] = None
    pdf_title: Optional[str] = None
    cache_result: Optional[CacheResult] = None  
    s3_url: Optional[str] = None
    flashcards: Optional[FlashcardSet] = None
    quiz: Optional[QuizSet] = None  # Add this line
    current_stage: str = "start"
    script: Optional[str] = None

class IntegratedContentGenerator:
    def __init__(self):
        self.content_engine = ContentEngine()
        self.rag_app = RAGApplication()
        self.podcast_cache = PodcastCache()
        self.s3_storage = S3Storage(bucket_name=os.getenv("AWS_BUCKET_NAME"))

def route_by_output_type(state: EnhancedGraphState) -> str:
    return state.output_type

TOPIC_EXPANSION_PROMPT = """You are an expert podcast planner. Create a detailed outline for a 3-5 minute 
podcast discussion between two speakers using the provided research context.

Topic: {topic}

Research Context:
{rag_context}

Focus on:
1. Breaking down complex concepts from the research
2. Including specific examples from the provided context
3. Natural conversation flow
4. Key insights and their implications

Current conversation: {messages}
"""

SCRIPT_GENERATION_PROMPT = """You are a professional podcast script writer. Create a natural conversation 
between two speakers based on the research context and outline provided.

Research Context:
{rag_context}

Guidelines:
- Speaker 1 is the host asks insightful questions and seeks clarification
- Speaker 2 asks is who explains the research findings
- Include natural elements like "umm", "hmm" for Speaker 2
- Don't Add [laughs], [sighs] for emotional moments
- Reference specific findings from the research
- Keep the tone conversational yet informative
- Ensure the script runs 3-5 minutes when read aloud
- Ensure natural flow between segments
- Keep the retrieved content accurate while making it engaging

Keep it as Speaker 1 and Speaker 2 only because don't add any names to it because I will parsing the script to make it work as 2 different Audios

Previous messages: {messages}
Outline: {outline}
"""

REFINE_SCRIPT_PROMPT = """Refine this research-based podcast script while maintaining its structure:
1. Keep the Speaker 1: and Speaker 2: format
2. Add appropriate pauses and emphasis
3. Ensure natural flow between segments
4. Maintain all expression markers like [laughs], [sighs]
5. Keep the research content accurate while making it engaging

Current script:
{script}

Return the enhanced script only, maintaining the exact same format."""

class PodcastGenerator:
    def __init__(self):
        self.llm = ChatOpenAI(
            model="learnlm-1.5-pro-experimental",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/", 
            temperature=0.7, 
            api_key=os.getenv("GEMINI_API_KEY")
        )
        self.rag_app = RAGApplication()
        self.elevenlabs_api_key = os.getenv("ELEVENLABS_API_KEY")
        self.voice_ids = {
            "Speaker 1": os.getenv("ELEVENLABS_VOICE_ID_1"),
            "Speaker 2": os.getenv("ELEVENLABS_VOICE_ID_2")
        }
        self.s3_storage = S3Storage(bucket_name=os.getenv("AWS_BUCKET_NAME"))
        self.cache = PodcastCache()
        self.content_engine = ContentEngine()
        self.quiz_generator = QuizGenerator(api_key=os.getenv("GEMINI_API_KEY"))
        
        self._validate_config()

    def _validate_config(self):
        if not self.elevenlabs_api_key:
            raise ValueError("ELEVENLABS_API_KEY is not set")
        if not all(self.voice_ids.values()):
            raise ValueError("Voice IDs not properly configured")
        if not all([
            os.getenv("AWS_ACCESS_KEY_ID"),
            os.getenv("AWS_SECRET_ACCESS_KEY"),
            os.getenv("AWS_BUCKET_NAME")
        ]):
            raise ValueError("AWS credentials not properly configured")
        if not all([
            os.getenv("UPSTASH_VECTOR_REST_URL"),
            os.getenv("UPSTASH_VECTOR_REST_TOKEN")
        ]):
            raise ValueError("Upstash credentials not properly configured")
        
    def create_graph(self):
        workflow = StateGraph(EnhancedGraphState)

        # Add nodes for all content types
        workflow.add_node("check_cache", self.check_cache)
        workflow.add_node("rag_retrieval", self.retrieve_context)
        workflow.add_node("topic_expansion", self.expand_topic)
        workflow.add_node("script_generation", self.generate_script)
        workflow.add_node("tts_generation", self.generate_tts)
        workflow.add_node("generate_flashcards", self.generate_flashcards)
        workflow.add_node("generate_quiz", self.generate_quiz)
        workflow.add_node("route_content", self.route_content)

        # Set up the base flow
        workflow.add_edge(START, "route_content")
        
        # Add single conditional edges block for content type routing
        workflow.add_conditional_edges(
            "route_content",
            lambda x: x.output_type,
            {
                "podcast": "check_cache",
                "flashcards": "generate_flashcards",
                "quiz": "generate_quiz"
            }
        )
        
        # Podcast generation path
        workflow.add_conditional_edges(
            "check_cache",
            self.route_from_cache,
            {
                "cached": "tts_generation",
                "not_cached": "rag_retrieval"
            }
        )
        
        workflow.add_edge("rag_retrieval", "topic_expansion")
        workflow.add_edge("topic_expansion", "script_generation")
        workflow.add_edge("script_generation", "tts_generation")
        
        # Final edges to END
        workflow.add_edge("tts_generation", END)
        workflow.add_edge("generate_flashcards", END)
        workflow.add_edge("generate_quiz", END)

        return workflow.compile()
    
    def generate_quiz(self, state: EnhancedGraphState) -> EnhancedGraphState:
        """Generate quiz using quiz generator"""
        context = f"""
        Question: {state.rag_context.question}
        Answer: {state.rag_context.answer}
        Evidence: {' '.join(state.rag_context.evidence)}
        """
        
        try:
            quiz_set = self.quiz_generator.generate_quiz(
                context=context,
                question=state.topic,
                num_questions=5
            )
            
            state.quiz = quiz_set
            state.current_stage = "complete"
            
        except Exception as e:
            print(f"Quiz generation error: {str(e)}")
            state.quiz = None
            
        return state
    
    def route_content(self, state: EnhancedGraphState) -> EnhancedGraphState:
        """Route to appropriate content generation based on output type"""
        state.current_stage = "routing"
        return state
    
    def generate_flashcards(self, state: EnhancedGraphState) -> EnhancedGraphState:
        """Generate flashcards using content engine"""
        context = f"""
        Question: {state.rag_context.question}
        Answer: {state.rag_context.answer}
        Evidence: {' '.join(state.rag_context.evidence)}
        """
        
        try:
            flashcard_set = self.content_engine.generate_flashcards(
                topic=state.topic,
                num=5,
                custom_instructions=f"Use this context to generate accurate flashcards:\n{context}"
            )
            
            state.flashcards = flashcard_set
            state.current_stage = "complete"
            
        except Exception as e:
            print(f"Flashcard generation error: {str(e)}")
            state.flashcards = None
            
        return state

    
    def generate_content(self, question: str, pdf_title: str, output_type: str = "podcast") -> Dict[str, Any]:
        """Generate either a podcast or flashcards based on the specified output type"""
        # First retrieve RAG context regardless of output type
        rag_response = self.rag_app.query_document(question, pdf_title)

        if output_type == "quiz":
            graph = self.create_graph()
            
            initial_state = EnhancedGraphState(
                messages=[HumanMessage(content=f"Create {output_type} about: {question}")],
                topic=question,
                output_type=output_type,
                rag_context=RAGContext(
                    question=question,
                    pdf_title=pdf_title,
                    answer=rag_response["answer"],
                    evidence=rag_response["relevant_chunks"]
                ),
                pdf_title=pdf_title,
                cache_result=None,
                s3_url=None,
                quiz=None
            )
            
            try:
                final_state = graph.invoke(initial_state)
                
                if final_state.get("quiz") is None:
                    raise ValueError("No quiz was generated")
                    
                quiz_dict = self.quiz_generator.format_quiz_for_display(final_state["quiz"])
                
                return {
                    "topic": question,
                    "quiz": quiz_dict,
                    "conversation_history": [m.content for m in final_state["messages"]],
                    "source_pdf": pdf_title,
                    "rag_context": {
                        "answer": rag_response["answer"],
                        "evidence": rag_response["relevant_chunks"]
                    }
                }
            except Exception as e:
                print(f"Error generating quiz: {str(e)}")
                raise
        
        if output_type == "flashcards":
            graph = self.create_graph()
            
            initial_state = EnhancedGraphState(
                messages=[HumanMessage(content=f"Create {output_type} about: {question}")],
                topic=question,
                output_type=output_type,
                rag_context=RAGContext(
                    question=question,
                    pdf_title=pdf_title,
                    answer=rag_response["answer"],
                    evidence=rag_response["relevant_chunks"]
                ),
                pdf_title=pdf_title,
                cache_result=None,
                s3_url=None,
                flashcards=None
            )
            
            try:
                final_state = graph.invoke(initial_state)
                
                if final_state.get("flashcards") is None:
                    raise ValueError("No flashcards were generated")
                    
                # Convert flashcards to dictionary format
                flashcards_dict = {
                    "title": final_state["flashcards"].title,
                    "flashcards": [
                        {
                            "front": card.front,
                            "back": card.back,
                            "explanation": card.explanation if hasattr(card, "explanation") else None
                        }
                        for card in final_state["flashcards"].flashcards
                    ]
                }
                
                return {
                    "topic": question,
                    "flashcards": flashcards_dict,
                    "conversation_history": [m.content for m in final_state["messages"]],
                    "source_pdf": pdf_title,
                    "rag_context": {
                        "answer": rag_response["answer"],
                        "evidence": rag_response["relevant_chunks"]
                    }
                }
            except Exception as e:
                print(f"Error generating flashcards: {str(e)}")
                raise
        else:  # podcast generation
            return self.generate_podcast(question=question, pdf_title=pdf_title)
        
        
        
       

    def check_cache(self, state: EnhancedGraphState) -> EnhancedGraphState:
        cached_result = self.cache.get_cached_podcast(
            state.rag_context.question, 
            state.rag_context.pdf_title
        )
        
        if cached_result:
            state.cache_result = CacheResult(
                found=True,
                data=cached_result
            )
            state.s3_url = cached_result.get("s3_url")
            state.script = cached_result.get("script")
            state.messages.append(
                AIMessage(content=f"Retrieved cached podcast: {state.s3_url}")
            )
        else:
            state.cache_result = CacheResult(found=False)
            
        state.current_stage = "cache_check"
        return state

    def route_from_cache(self, state: EnhancedGraphState) -> str:
        return "cached" if state.cache_result and state.cache_result.found else "not_cached"


    def retrieve_context(self, state: EnhancedGraphState) -> EnhancedGraphState:
        if not state.rag_context or not state.pdf_title:
            raise ValueError("RAG context or PDF title not provided")
            
        rag_response = self.rag_app.query_document(
            state.rag_context.question, 
            state.pdf_title
        )
        
        state.rag_context.answer = rag_response["answer"]
        state.rag_context.evidence = rag_response["relevant_chunks"]
        
        context_message = f"""Research Context:
        Answer: {rag_response['answer']}
        Evidence: {' '.join(rag_response['relevant_chunks'])}"""
        
        state.messages.append(AIMessage(content=context_message))
        state.current_stage = "rag_retrieval"
        return state
            

    def expand_topic(self, state: EnhancedGraphState) -> EnhancedGraphState:
        prompt = ChatPromptTemplate.from_template(TOPIC_EXPANSION_PROMPT)
        
        rag_context = f"""Answer: {state.rag_context.answer}
        Evidence: {' '.join(state.rag_context.evidence)}"""
        
        formatted_prompt = prompt.format_messages(
            topic=state.topic,
            rag_context=rag_context,
            messages="\n".join([msg.content for msg in state.messages])
        )
        
        response = self.llm.invoke(formatted_prompt)
        state.messages.append(AIMessage(content=response.content))
        state.current_stage = "topic_expansion"
        return state

    def generate_script(self, state: EnhancedGraphState) -> EnhancedGraphState:
        prompt = ChatPromptTemplate.from_template(SCRIPT_GENERATION_PROMPT)
        
        rag_context = f"""Answer: {state.rag_context.answer}
        Evidence: {' '.join(state.rag_context.evidence)}"""
        
        formatted_prompt = prompt.format_messages(
            rag_context=rag_context,
            messages="\n".join([msg.content for msg in state.messages]),
            outline=state.messages[-1].content
        )
        
        response = self.llm.invoke(formatted_prompt)
        
        try:
            script_structured = PodcastScript.parse_raw(response.content)
            state.script = script_structured.json()
        except:
            state.script = response.content
            
        state.messages.append(AIMessage(content=response.content))
        state.current_stage = "script_generation"
        return state

    def generate_tts(self, state: EnhancedGraphState) -> EnhancedGraphState:
        if state.cache_result and state.cache_result.found:
            state.current_stage = "complete"
            return state
            
        if not state.script:
            raise ValueError("No script available for TTS generation.")
        
        try:
            script = PodcastScript.parse_raw(state.script)
        except:
            script = self.parse_unstructured_script(state.script)
        
        audio_segments = []
        for segment in script.segments:
            voice_id = self.voice_ids.get(segment.speaker)
            audio_data = self.synthesize_speech(segment.text, voice_id)
            audio_segments.append(audio_data)
        
        combined_audio = AudioSegment.empty()
        for audio in audio_segments:
            combined_audio += audio + AudioSegment.silent(duration=500)
        
        temp_file = f"temp_podcast_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp3"
        combined_audio.export(temp_file, format="mp3")
        
        try:
            s3_url = self.s3_storage.upload_file(
                file_path=temp_file,
                podcast_title=state.topic,
                pdf_title=state.pdf_title
            )
            
            self.cache.cache_podcast(
                state.rag_context.question,
                state.pdf_title,
                {
                    "topic": state.topic,
                    "script": state.script,
                    "conversation_history": [m.content for m in state.messages],
                    "source_pdf": state.pdf_title,
                    "s3_url": s3_url,
                    "rag_context": {
                        "answer": state.rag_context.answer,
                        "evidence": state.rag_context.evidence
                    }
                }
            )
            
            state.s3_url = s3_url
            state.messages.append(
                AIMessage(content=f"Podcast audio generated and uploaded to S3: {s3_url}")
            )
            
        except Exception as e:
            print(f"Warning: S3 upload failed - {str(e)}")
            state.messages.append(
                AIMessage(content=f"Warning: S3 upload failed. Podcast saved locally as {temp_file}")
            )
        
        state.current_stage = "complete"
        return state

    def parse_unstructured_script(self, script_text: str) -> PodcastScript:
        segments = []
        for line in script_text.split('\n'):
            if line.strip() == "":
                continue
            if line.startswith("Speaker 1:"):
                speaker = "Speaker 1"
                text = line.replace("Speaker 1:", "").strip()
            elif line.startswith("Speaker 2:"):
                speaker = "Speaker 2"
                text = line.replace("Speaker 2:", "").strip()
            else:
                speaker = "Speaker 1"
                text = line.strip()
            segments.append(PodcastSegment(speaker=speaker, text=text))
        return PodcastScript(segments=segments)

    def synthesize_speech(self, text: str, voice_id: str) -> AudioSegment:
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"
        headers = {
            "Accept": "audio/mpeg",
            "Content-Type": "application/json",
            "xi-api-key": self.elevenlabs_api_key
        }
        data = {
            "text": text,
            "model_id": "eleven_turbo_v2_5",
            "voice_settings": {
                "stability": 0.5,
                "similarity_boost": 0.75
            }
        }
        
        response = requests.post(url, headers=headers, json=data)
        
        if response.status_code != 200:
            raise ValueError(f"ElevenLabs API Error: {response.status_code} - {response.text}")
        
        audio = AudioSegment.from_file(io.BytesIO(response.content), format="mp3")
        return audio
    
    def generate_podcast(self, question: str, pdf_title: str) -> Dict[str, Any]:
        """Generate a podcast based on the provided question and PDF"""
        initial_state = EnhancedGraphState(
            messages=[HumanMessage(content=f"Create a podcast about: {question}")],
            topic=question,
            output_type="podcast",
            rag_context=RAGContext(
                question=question,
                pdf_title=pdf_title
            ),
            pdf_title=pdf_title,
            cache_result=None,
            s3_url=None,
            current_stage="start"
        )
        
        try:
            graph = self.create_graph()
            final_state = graph.invoke(initial_state)
            
            return {
                "topic": question,
                "script": final_state["script"],
                "conversation_history": [m.content for m in final_state["messages"]],
                "source_pdf": pdf_title,
                "s3_url": final_state["s3_url"],
                "cached": final_state["cache_result"].found if final_state["cache_result"] else False,
                "rag_context": {
                    "answer": final_state["rag_context"].answer,
                    "evidence": final_state["rag_context"].evidence
                } if not final_state["cache_result"].found else None
            }
        except Exception as e:
            print(f"Error in podcast generation: {str(e)}")
            raise

    

    def process_document(self, pdf_path: str) -> bool:
        return self.rag_app.process_document(pdf_path)

    def list_available_pdfs(self) -> List[str]:
        return self.rag_app.list_available_pdfs()

def display_menu():
    print("\nRAG Podcast Generator Menu:")
    print("1. List available documents")
    print("2. Index new document")
    print("3. Generate podcast from existing document")
    print("4. Exit")
    print("-" * 50)

def handle_index_document(generator: PodcastGenerator):
    """Handle document indexing with improved path handling"""
    pdf_path = input("\nEnter the path to the PDF file: ").strip()
    
    try:
        expanded_path = os.path.expanduser(pdf_path)
        cleaned_path = pdf_path.replace('\\', '')
        
        # Try different path variations
        for path in [pdf_path, expanded_path, cleaned_path]:
            if os.path.exists(path):
                print(f"\nProcessing document: {path}")
                if generator.process_document(path):
                    print("Document successfully indexed!")
                    return
                
        print("Error: File not found. Please check the path and try again.")
        print("Tips for entering file paths:")
        print("1. Use forward slashes (/) instead of backslashes")
        print("2. You can drag and drop the file into the terminal")
        
    except Exception as e:
        print(f"Error processing document: {str(e)}")

def handle_podcast_generation(generator: PodcastGenerator):
    """Handle the podcast generation process"""
    pdfs = generator.list_available_pdfs()
    if not pdfs:
        print("\nNo documents available. Please index some documents first.")
        return
        
    print("\nAvailable PDFs:")
    for i, pdf in enumerate(pdfs, 1):
        print(f"{i}. {pdf}")
        
    while True:
        try:
            selection = int(input("\nSelect PDF number: ")) - 1
            if 0 <= selection < len(pdfs):
                selected_pdf = pdfs[selection]
                break
            print("Invalid selection. Please try again.")
        except ValueError:
            print("Please enter a valid number.")
            
    question = input("\nEnter your question about the document: ").strip()
    
    print(f"\nProcessing query: {question}")
    print(f"Using PDF: {selected_pdf}")
    
    try:
        result = generator.generate_podcast(question, selected_pdf)
        
        if result.get('cached'):
            print("\nRetrieved cached podcast!")
        else:
            print("\nGenerated new podcast!")
            
        print(f"S3 URL: {result['s3_url']}")
        
        # Save result to local file for reference
        with open("podcast_output.json", "w") as f:
            json.dump(result, f, indent=2)
            
        print("\nResults saved to podcast_output.json")
        
    except Exception as e:
        print(f"\nError generating podcast: {str(e)}")

def handle_content_generation(generator: PodcastGenerator):
    """Handle the content generation process with enhanced user interaction and quiz features"""
    pdfs = generator.list_available_pdfs()
    if not pdfs:
        print("\nNo documents available. Please index some documents first.")
        return
        
    print("\nAvailable PDFs:")
    for i, pdf in enumerate(pdfs, 1):
        print(f"{i}. {pdf}")
        
    while True:
        try:
            selection = int(input("\nSelect PDF number: ")) - 1
            if 0 <= selection < len(pdfs):
                selected_pdf = pdfs[selection]
                break
            print("Invalid selection. Please try again.")
        except ValueError:
            print("Please enter a valid number.")
            
    question = input("\nEnter your question about the document: ").strip()
    
    print("\nSelect output type:")
    print("1. Podcast")
    print("2. Flashcards")
    print("3. Quiz")
    
    while True:
        try:
            output_choice = int(input("Enter your choice (1-3): "))
            if output_choice in [1, 2, 3]:
                output_type = {
                    1: "podcast",
                    2: "flashcards",
                    3: "quiz"
                }[output_choice]
                break
            print("Invalid selection. Please try again.")
        except ValueError:
            print("Please enter a valid number.")
    
    print(f"\nProcessing query: {question}")
    print(f"Using PDF: {selected_pdf}")
    print(f"Generating: {output_type}")
    
    try:
        result = generator.generate_content(question, selected_pdf, output_type)
        
        if output_type == "podcast":
            if result.get('cached'):
                print("\nRetrieved cached podcast!")
            else:
                print("\nGenerated new podcast!")
            print(f"S3 URL: {result['s3_url']}")
            
        elif output_type == "flashcards":
            print("\nGenerated flashcards!")
            if result.get('flashcards'):
                print("\nFlashcards:")
                for i, card in enumerate(result['flashcards']['flashcards'], 1):
                    print(f"\nCard {i}:")
                    print(f"Front: {card['front']}")
                    print(f"Back: {card['back']}")
                    if card.get('explanation'):
                        print(f"Explanation: {card['explanation']}")
                        
        elif output_type == "quiz":
            print("\nGenerated quiz!")
            if result.get('quiz'):
                quiz_data = result['quiz']
                print(f"\nQuiz: {quiz_data['title']}")
                print(f"Description: {quiz_data['description']}")
                print(f"Total Points: {quiz_data['total_points']}")
                print(f"Recommended Time: {quiz_data['recommended_time']} minutes")
                
                # Interactive quiz taking
                take_quiz = input("\nWould you like to take the quiz now? (y/n): ").lower().strip() == 'y'
                
                if take_quiz:
                    user_answers = []
                    score = 0
                    total_questions = len(quiz_data['questions'])
                    
                    print("\n=== Quiz Started ===")
                    for i, q in enumerate(quiz_data['questions'], 1):
                        print(f"\nQuestion {i} of {total_questions} ({q['difficulty']}):")
                        print(f"Q: {q['question']}")
                        print("\nOptions:")
                        for j, opt in enumerate(q['options'], 1):
                            print(f"  {j}. {opt}")
                            
                        while True:
                            try:
                                answer = int(input("\nEnter your answer (1-4): "))
                                if 1 <= answer <= 4:
                                    user_answers.append(q['options'][answer-1])
                                    break
                                print("Please enter a number between 1 and 4.")
                            except (ValueError, IndexError):
                                print("Invalid input. Please enter a number between 1 and 4.")
                    
                    # Grade the quiz
                    grading_result = generator.quiz_generator.grade_quiz(
                        quiz=result['quiz'],
                        user_answers=user_answers
                    )
                    
                    # Display results
                    print("\n=== Quiz Results ===")
                    print(f"Score: {grading_result['score']:.1f}%")
                    print(f"Correct Answers: {grading_result['correct_count']} out of {grading_result['total_questions']}")
                    
                    show_feedback = input("\nWould you like to see detailed feedback? (y/n): ").lower().strip() == 'y'
                    if show_feedback:
                        print("\n=== Detailed Feedback ===")
                        for fb in grading_result['feedback']:
                            q_num = fb['question_num']
                            q_data = quiz_data['questions'][q_num-1]
                            print(f"\nQuestion {q_num}:")
                            print(f"Your Answer: {fb['user_answer']}")
                            print(f"Correct Answer: {fb['correct_answer']}")
                            if not fb['correct']:
                                print(f"Explanation: {q_data['explanation']}")
                else:
                    # Just display the quiz without taking it
                    print("\nQuiz Questions Preview:")
                    for i, q in enumerate(quiz_data['questions'], 1):
                        print(f"\nQuestion {i} ({q['difficulty']}):")
                        print(f"Q: {q['question']}")
                        print("Options:")
                        for j, opt in enumerate(q['options'], 1):
                            print(f"  {j}. {opt}")
        
        # Save result to local file for reference
        output_filename = f"{output_type}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_output.json"
        with open(output_filename, "w") as f:
            json.dump(result, f, indent=2)
            
        print(f"\nResults saved to {output_filename}")
        
    except Exception as e:
        print(f"\nError generating {output_type}: {str(e)}")
        print("Please try again or choose a different output type.")

def main():
    """Main execution loop"""
    try:
        print("Initializing Content Generator...")
        generator = PodcastGenerator()
        
        while True:
            display_menu()
            choice = input("Enter your choice (1-4): ").strip()
            
            if choice == "1":
                pdfs = generator.list_available_pdfs()
                print("\nAvailable PDFs:")
                if not pdfs:
                    print("No documents indexed yet.")
                else:
                    for pdf in pdfs:
                        print(f"- {pdf}")
                        
            elif choice == "2":
                handle_index_document(generator)
                
            elif choice == "3":
                handle_content_generation(generator)
                
            elif choice == "4":
                print("\nExiting Content Generator...")
                break
                
            else:
                print("\nInvalid choice. Please select 1-4.")
                
    except KeyboardInterrupt:
        print("\n\nOperation cancelled by user. Exiting...")
    except Exception as e:
        print(f"\nAn unexpected error occurred: {str(e)}")
        raise

if __name__ == "__main__":
    main()


