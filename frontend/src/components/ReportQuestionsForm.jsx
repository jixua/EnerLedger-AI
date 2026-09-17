import { useState } from "react";
import { Loader2 } from "lucide-react";

function isAnswerMissing(question, value) {
  if (question.field_type === "date_range") return !value?.start || !value?.end;
  return !String(value ?? "").trim();
}

function normalizeAnswerValue(question, value) {
  if (question.field_type === "number" || question.field_type === "integer") {
    return Number(value);
  }
  if (question.field_type === "array") {
    return String(value ?? "")
      .split(/[\n,，]/)
      .map((item) => item.trim())
      .filter(Boolean);
  }
  return value;
}

/**
 * 报告补充问题的作答表单（生成报告对话框与对话页报告卡片共用）。
 * 答案会以 USER_INPUT 身份进入报告证据链，不会被当作源文档事实。
 */
export function ReportQuestionsForm({ questions, submitting, error, submitLabel = "提交并继续生成", onSubmit }) {
  const [answers, setAnswers] = useState({});
  const [validationError, setValidationError] = useState("");

  if (!questions?.length) return null;

  const update = (questionId, value) => {
    setAnswers((current) => ({ ...current, [questionId]: value }));
  };

  function handleSubmit(event) {
    event.preventDefault();
    if (submitting) return;
    if (questions.some((question) => question.required && isAnswerMissing(question, answers[question.question_id]))) {
      setValidationError("请完成所有必填补充项。");
      return;
    }
    setValidationError("");
    onSubmit?.(
      questions
        .filter((question) => question.required || String(answers[question.question_id] ?? "").trim())
        .map((question) => ({
          question_id: question.question_id,
          value: normalizeAnswerValue(question, answers[question.question_id]),
          notes: null,
        })),
    );
  }

  return (
    <form className="report-question-form" onSubmit={handleSubmit}>
      <div className="report-question-form__heading">
        <strong>需要补充 {questions.length} 项信息</strong>
        <span>回答将标记为 USER_INPUT，不会改写为源文档事实。</span>
      </div>
      {questions.map((question) => (
        <label key={question.question_id} className="form-field">
          <span>{question.question} {question.required ? <b>*</b> : null}</span>
          {question.options?.length ? (
            <select
              value={answers[question.question_id] || ""}
              onChange={(event) => update(question.question_id, event.target.value)}
            >
              <option value="">请选择</option>
              {question.options.map((option) => (
                <option key={String(option.value)} value={option.value}>{option.label}</option>
              ))}
            </select>
          ) : question.field_type === "date_range" ? (
            <span className="report-date-range">
              <input
                type="date"
                value={answers[question.question_id]?.start || ""}
                onChange={(event) => update(question.question_id, { ...(answers[question.question_id] || {}), start: event.target.value })}
              />
              <input
                type="date"
                value={answers[question.question_id]?.end || ""}
                onChange={(event) => update(question.question_id, { ...(answers[question.question_id] || {}), end: event.target.value })}
              />
            </span>
          ) : question.field_type === "array" ? (
            <textarea
              rows="3"
              value={answers[question.question_id] || ""}
              onChange={(event) => update(question.question_id, event.target.value)}
              placeholder="每行填写一项"
            />
          ) : (
            <input
              type={["number", "integer"].includes(question.field_type) ? "number" : question.field_type === "date" ? "date" : "text"}
              value={answers[question.question_id] || ""}
              onChange={(event) => update(question.question_id, event.target.value)}
            />
          )}
          <small>字段：{question.field_label || question.field_id}</small>
        </label>
      ))}
      {validationError ? <p className="form-error" role="alert">{validationError}</p> : null}
      {error ? <p className="form-error" role="alert">{error}</p> : null}
      <button type="submit" className="button button--primary" disabled={submitting}>
        {submitting ? <Loader2 className="spin" size={15} /> : null}
        {submitting ? "正在提交" : submitLabel}
      </button>
    </form>
  );
}

export default ReportQuestionsForm;
