'use client';

import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { FileLayout } from "@/components/layout/file-layout";
import { QuizResults, AnswersReview } from "@/components/quiz";
import { useQuizStore } from "@/store/quiz-store";
import { useToast } from "@/hooks/use-toast";
import { Button } from "@/components/ui/button";
import { ChevronLeft } from "lucide-react";

interface QuizResultsPageProps {
  params: {
    fileId: string;
    quizId: string;
    attemptId: string;
  };
}

export default function QuizResultsPage({ params }: QuizResultsPageProps) {
  const router = useRouter();
  const { toast } = useToast();
  const {
    currentQuiz,
    currentAttempt,
    questions,
    responses,
    startQuiz,
    reset
  } = useQuizStore();

  const [showAnswers, setShowAnswers] = useState(false);

  if (!currentQuiz || !currentAttempt) {
    router.push(`/dashboard/${params.fileId}/quiz`);
    return null;
  }

  const handleRetry = async () => {
    try {
      await startQuiz(params.quizId);
      router.push(`/dashboard/${params.fileId}/quiz/${params.quizId}/attempt`);
    } catch (err) {
      toast({
        title: "Error",
        description: "Failed to start quiz. Please try again.",
        variant: "destructive"
      });
    }
  };

  const handleReturn = () => {
    reset(); // Clear quiz state
    router.push(`/dashboard/${params.fileId}/quiz`);
  };

  return (
    <FileLayout fileId={params.fileId}>
      <div className="space-y-6">
        {/* Back Button */}
        <Button
          variant="ghost"
          className="gap-2"
          onClick={handleReturn}
        >
          <ChevronLeft className="h-4 w-4" />
          Back to Quizzes
        </Button>

        {/* Main Content */}
        <div className="max-w-4xl mx-auto">
          {!showAnswers ? (
            <QuizResults
              quiz={currentQuiz}
              attempt={currentAttempt}
              onViewAnswers={() => setShowAnswers(true)}
              onRetry={handleRetry}
              onReturn={handleReturn}
            />
          ) : (
            <div className="space-y-6">
              <Button
                variant="ghost"
                className="gap-2"
                onClick={() => setShowAnswers(false)}
              >
                <ChevronLeft className="h-4 w-4" />
                Back to Summary
              </Button>

              <AnswersReview
                questions={questions}
                responses={responses}
              />
            </div>
          )}
        </div>
      </div>
    </FileLayout>
  );
}