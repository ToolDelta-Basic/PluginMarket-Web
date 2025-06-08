document.addEventListener('DOMContentLoaded', function() {
    // 简单的安装按钮效果
    document.querySelectorAll('.btn-install, .btn-install-package').forEach(button => {
        button.addEventListener('click', function() {
            const originalText = this.innerHTML;
            this.innerHTML = '<i class="bi bi-check2"></i> 已安装';
            this.disabled = true;
            
            setTimeout(() => {
                this.innerHTML = originalText;
                this.disabled = false;
            }, 2000);
        });
    });
    
    // 滚动时导航栏效果
    window.addEventListener('scroll', function() {
        const navbar = document.querySelector('.navbar');
        if (window.scrollY > 50) {
            navbar.style.padding = '10px 0';
            navbar.style.boxShadow = '0 4px 20px rgba(0, 0, 0, 0.5)';
        } else {
            navbar.style.padding = '15px 0';
            navbar.style.boxShadow = 'none';
        }
    });
    
    // 筛选按钮效果
    document.querySelectorAll('.filter-btn').forEach(button => {
        button.addEventListener('click', function() {
            document.querySelectorAll('.filter-btn').forEach(btn => {
                btn.classList.remove('active');
            });
            this.classList.add('active');
            
            // 这里可以添加实际的筛选逻辑
            console.log('筛选: ' + this.textContent);
        });
    });
    
    // 搜索功能
    const searchInput = document.querySelector('.search-box input');
    searchInput.addEventListener('input', function() {
        const searchTerm = this.value.toLowerCase();
        
        // 插件搜索
        document.querySelectorAll('.plugin-card').forEach(card => {
            const name = card.querySelector('.plugin-name').textContent.toLowerCase();
            const author = card.querySelector('.plugin-author').textContent.toLowerCase();
            const description = card.querySelector('.plugin-description').textContent.toLowerCase();
            
            if (name.includes(searchTerm) || author.includes(searchTerm) || description.includes(searchTerm)) {
                card.style.display = 'flex';
            } else {
                card.style.display = 'none';
            }
        });
        
        // 整合包搜索
        document.querySelectorAll('.package-card').forEach(card => {
            const name = card.querySelector('.package-name').textContent.toLowerCase();
            const author = card.querySelector('.package-author').textContent.toLowerCase();
            const description = card.querySelector('.package-description').textContent.toLowerCase();
            
            if (name.includes(searchTerm) || author.includes(searchTerm) || description.includes(searchTerm)) {
                card.style.display = 'block';
            } else {
                card.style.display = 'none';
            }
        });
    });
});