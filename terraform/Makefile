.PHONY: init plan apply destroy kubeconfig

# ── Phase 0: Foundation ───────────────────────────────────────────────────────

init:
	cd terraform && terraform init

plan:
	cd terraform && terraform plan -var-file=terraform.tfvars

apply:
	cd terraform && terraform apply -var-file=terraform.tfvars

destroy:
	cd terraform && terraform destroy -var-file=terraform.tfvars

# Configure kubectl after cluster is up
kubeconfig:
	aws eks update-kubeconfig \
		--name $$(cd terraform && terraform output -raw cluster_name) \
		--region us-east-1

# Verify cluster is healthy
verify:
	kubectl get nodes -o wide
	kubectl get pods -A
