'use client';

import { useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { FileLayout } from "@/components/layout/file-layout";
import { QuestionView, QuizProgress } from "@/components/quiz";
import { useQuizStore } from "@/store/quiz-store";
import { useToast } from "@/hooks/use-toast";
import { Button } from "@/components/ui/button";
import { ChevronLeft } from "lucide-react";

interface QuizAttemptPageProps {
  params: {
    fileId: string;
    quizId: string;
  };
}

export default function QuizAttemptPage({ params }: QuizAttemptPageProps) {
  const router = useRouter();
  const { toast } = useToast();
  const {
    currentQuiz,
    currentAttempt,
    questions,
    currentQuestionIndex,
    responses,
    isLoading,
    error,
    startQuiz,
    submitResponse,
    completeQuiz
  } = useQuizStore();

  useEffect(() => {
    const initializeQuiz = async () => {
      try {
        // If we don't have an active attempt, start one
        if (!currentAttempt) {
          await startQuiz(params.quizId);
        }
      } catch (err) {
        toast({
          title: "Error",
          description: "Failed to start quiz. Please try again.",
          variant: "destructive"
        });
        router.push(`/dashboard/${params.fileId}/quiz`);
      }
    };

    initializeQuiz();
  }, [params.quizId, currentAttempt, startQuiz, router, params.fileId, toast]);

  const currentQuestion = questions[currentQuestionIndex];
  const isLastQuestion = currentQuestionIndex === questions.length - 1;

  const handleSubmitResponse = async (response: string) => {
    if (!currentQuestion) return;

    try {
      const startTime = performance.now();
      await submitResponse(
        currentQuestion.id,
        response,
        Math.floor((performance.now() - startTime) / 1000) // time in seconds
      );
    } catch (err) {
      toast({
        title: "Error",
        description: "Failed to submit answer. Please try again.",
        variant: "destructive"
      });
    }
  };

  const handleNext = async () => {
    // If it's the last question, complete the quiz
    if (isLastQuestion) {
      try {
        const completedAttempt = await completeQuiz();
        // Navigate to results page (we'll create this later)
        router.push(
          `/dashboard/${params.fileId}/quiz/${params.quizId}/attempt/${completedAttempt.id}/results`
        );
      } catch (err) {
        toast({
          title: "Error",
          description: "Failed to complete quiz. Please try again.",
          variant: "destructive"
        });
      }
    }
  };

  if (!currentQuestion || !currentAttempt) {
    return null; // or a loading state
  }

  return (
    <FileLayout fileId={params.fileId}>
      <div className="space-y-6">
        {/* Header with Back Button */}
        <div className="flex items-center justify-between">
          <Button
            variant="ghost"
            className="gap-2"
            onClick={() => router.push(`/dashboard/${params.fileId}/quiz`)}
          >
            <ChevronLeft className="h-4 w-4" />
            Back to Quizzes
          </Button>

          {currentQuiz && (
            <h2 className="text-lg font-semibold">
              {currentQuiz.title}
            </h2>
          )}
        </div>

        {/* Progress Bar */}
        <QuizProgress
          currentQuestionIndex={currentQuestionIndex}
          totalQuestions={questions.length}
          responses={responses}
          className="max-w-3xl mx-auto"
        />

        {/* Question View */}
        <QuestionView
          question={currentQuestion}
          response={responses[currentQuestion.id]}
          onSubmit={handleSubmitResponse}
          onNext={handleNext}
          isLast={isLastQuestion}
        />

        {/* Error Message */}
        {error && (
          <div className="text-center text-destructive">
            {error}
          </div>
        )}
      </div>
    </FileLayout>
  );
}