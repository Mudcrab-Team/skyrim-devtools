; SPDX-License-Identifier: GPL-3.0-or-later
; Engine function hooks for the tracer. MinHook points each hooked function at a 16-byte stub that loads the
; hook index into r10d and jumps to one shared thunk. The thunk times the original call: it saves the four
; register arguments, asks devtools_trace_begin for a start tick, copies the caller's stack arguments into its
; own frame, calls the original and hands the result to devtools_trace_end.
;
; The thunk is a PROC FRAME with unwind codes, so the sampler (which walks only through .pdata) carries on from a
; traced function into its game callers. A thrown exception unwinds through it like any frame and only loses that
; one range. Functions taking more than 16 stack arguments are not supported; none in the hook list do.

EXTERN devtools_trace_begin:PROC
EXTERN devtools_trace_end:PROC
EXTERN devtools_trace_originals:QWORD

STACK_ARGS EQU 16
FRAME_SIZE EQU 264          ; keeps rsp 16-byte aligned after the two pushes
ARGS_OUT   EQU 32           ; stack arguments passed on to the original
XMM_SAVE   EQU 160          ; xmm0-xmm3
GPR_SAVE   EQU 224          ; rcx, rdx, r8, r9
ARGS_IN    EQU FRAME_SIZE + 16 + 8 + 32   ; past saved rdi/rbx, return address and caller's home space

.code

devtools_trace_thunk PROC FRAME
    push rbx
    .pushreg rbx
    push rdi
    .pushreg rdi
    sub rsp, FRAME_SIZE
    .allocstack FRAME_SIZE
    .endprolog

    mov ebx, r10d
    mov [rsp + GPR_SAVE], rcx
    mov [rsp + GPR_SAVE + 8], rdx
    mov [rsp + GPR_SAVE + 16], r8
    mov [rsp + GPR_SAVE + 24], r9
    movdqu [rsp + XMM_SAVE], xmm0
    movdqu [rsp + XMM_SAVE + 16], xmm1
    movdqu [rsp + XMM_SAVE + 32], xmm2
    movdqu [rsp + XMM_SAVE + 48], xmm3

    mov ecx, ebx
    call devtools_trace_begin
    mov rdi, rax

    argIndex = 0
    REPEAT STACK_ARGS
        mov rax, [rsp + ARGS_IN + argIndex * 8]
        mov [rsp + ARGS_OUT + argIndex * 8], rax
        argIndex = argIndex + 1
    ENDM

    movdqu xmm0, [rsp + XMM_SAVE]
    movdqu xmm1, [rsp + XMM_SAVE + 16]
    movdqu xmm2, [rsp + XMM_SAVE + 32]
    movdqu xmm3, [rsp + XMM_SAVE + 48]
    mov rcx, [rsp + GPR_SAVE]
    mov rdx, [rsp + GPR_SAVE + 8]
    mov r8, [rsp + GPR_SAVE + 16]
    mov r9, [rsp + GPR_SAVE + 24]
    lea rax, devtools_trace_originals
    mov rax, [rax + rbx * 8]
    call rax

    mov [rsp + GPR_SAVE], rax
    movdqu [rsp + XMM_SAVE], xmm0
    mov ecx, ebx
    mov rdx, rdi
    call devtools_trace_end
    mov rax, [rsp + GPR_SAVE]
    movdqu xmm0, [rsp + XMM_SAVE]

    add rsp, FRAME_SIZE
    pop rdi
    pop rbx
    ret
devtools_trace_thunk ENDP

; One stub per hook slot, 16 bytes apart; trace.cpp checks each stub's index before using it.
    ALIGN 16
PUBLIC devtools_trace_stubs
devtools_trace_stubs LABEL BYTE
    stubIndex = 0
    REPEAT 128
        ALIGN 16
        mov r10d, stubIndex
        jmp devtools_trace_thunk
        stubIndex = stubIndex + 1
    ENDM

END
